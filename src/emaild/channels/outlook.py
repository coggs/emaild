"""Outlook.com and Microsoft 365 connector (Microsoft Graph), read-only: delegated `Mail.Read` + `User.Read` +
`offline_access`.

OAuth 2.0 authorisation code + PKCE against the Microsoft identity platform, implemented directly on httpx - no MSAL.
Refresh tokens rotate: every refresh hands the new credentials to `on_credentials` straight away so they are
persisted even if the rest of the cycle fails.

Authorities: personal accounts (Outlook.com, Hotmail) use EMAILD_MS_TENANT (`consumers` by default); work or school
accounts (Microsoft 365 / Entra ID) use EMAILD_MS_WORK_TENANT (`organizations` by default, or one tenant's GUID or
verified domain). The authority an account was linked with is stored on it (`accounts.ms_tenant`, NULL = the personal
default) and its refreshes go to that same authority: a refresh token is only valid at the authority that issued it.
Graph itself behaves the same for both kinds (same folders, delta and labels).

Sync model
----------
Folders synced: Inbox, Sent Items, Junk Email, and Archive when the mailbox has one (well-known names `inbox`,
`sentitems`, `junkemail`, `archive`). Deleted Items is never synced, only resolved so moves into it can be labelled.

Each folder is tracked with a Graph delta query (`/me/mailFolders/{folder}/messages/delta`). The *first* round is
filtered to `receivedDateTime ge <now - EMAILD_BACKFILL_DAYS>`, so it is the backfill: it pages through exactly
what `GET /messages?$filter=receivedDateTime ge ...` would list, and ends with a deltaLink that only tracks that
window onwards. Backfilling with the plain list endpoint and then starting delta would enumerate the window twice.
`list_page` (the plain list endpoint, nextLink paging) is still used by `emaild verify`.

Progress lives in `accounts.sync_state` (JSON): `{"folders": {name: id}, "sync": {name: {"next"|"delta": url}}}`.
A link only advances once its page is fully processed, so a budget stop or crash re-reads the same page (stored
messages are skipped). An expired delta token (410 Gone / syncStateNotFound / resyncRequired) restarts that folder's
filtered round - a bounded re-backfill.

Message ids: every request sends `Prefer: IdType="ImmutableId"`. Default Outlook ids change when a message moves
between folders; immutable ids don't, so a Junk move updates the stored row instead of looking like delete + new.

Label mapping (onto the Gmail label names the rest of emAIl already understands)
----------------------------------------------------------------------------------
    parent folder inbox          -> INBOX
    parent folder sentitems      -> SENT       (store: is_from_me)
    parent folder junkemail      -> SPAM       (store.update_labels reclassifies; search/brief exclude)
    parent folder deleteditems   -> TRASH      (search excludes)
    parent folder archive/other  -> (none)     (like an archived Gmail message)
    isRead == false              -> UNREAD
    flag.flagStatus == flagged   -> STARRED
    importance == high           -> IMPORTANT
    inferenceClassification other-> CATEGORY_OTHER  (an organisation may disable Focused Inbox: a missing
                                                     value is simply no label)
Focused Inbox's "Other" holds plenty of non-promotional mail (notifications, newsletters you read), so it is *not*
mapped to CATEGORY_PROMOTIONS; bulk detection keeps relying on List-Unsubscribe/List-Id/Precedence headers, which
the raw MIME carries exactly as for Gmail.

Deletions: `@removed` in a folder's delta means "no longer in this folder". For a stored message we look it up
once: 404 -> deleted (store.mark_deleted, as for Gmail), otherwise relabel from its new folder.

Throttling: 429/503/504 are retried up to MAX_RETRIES times honouring Retry-After (capped at MAX_WAIT seconds,
beyond which the cycle stops with RateLimited). Every attempt - Graph and token endpoint - is charged to the
per-cycle Budget.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlencode

import httpx

from ..config import settings
from ..models import Item
from ..normalise import parse_mime
from .base import Budget, Changes, CursorExpired, Page, RateLimited, ReauthRequired

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Mail.Read"]
SYNC_FOLDERS = ("inbox", "sentitems", "junkemail", "archive")
KNOWN_FOLDERS = SYNC_FOLDERS + ("deleteditems",)
OPTIONAL_FOLDERS = {"archive"}
FOLDER_LABELS = {"inbox": "INBOX", "sentitems": "SENT", "junkemail": "SPAM", "deleteditems": "TRASH"}
SELECT = ("id,conversationId,internetMessageId,receivedDateTime,isRead,importance,flag,"
          "inferenceClassification,parentFolderId,bodyPreview")
PAGE_SIZE = 50
MAX_RETRIES = 3
MAX_WAIT = 60.0
RETRY_STATUSES = {429, 503, 504}
EXPIRED_CODES = {"syncStateNotFound", "syncStateInvalid", "resyncRequired"}
REAUTH_ERRORS = {"invalid_grant", "interaction_required", "consent_required"}
REAUTH_MESSAGE = "Microsoft sign-in expired — relink the account"
IMMUTABLE = 'IdType="ImmutableId"'
PERSONAL_TENANTS = ("consumers",)
KINDS = ("personal", "work")


class TokenError(RuntimeError):
    """The token endpoint refused a request for a reason relinking won't fix (bad client id/secret, etc.)."""


@dataclass(frozen=True)
class MsApp:
    client_id: str
    client_secret: str
    tenant: str = "consumers"

    @classmethod
    def from_settings(cls) -> "MsApp":
        s = settings()
        return cls(s.ms_client_id, s.ms_client_secret, s.ms_tenant or "consumers")

    @classmethod
    def for_kind(cls, kind: str) -> "MsApp":
        """'personal' -> EMAILD_MS_TENANT (consumers); 'work' -> EMAILD_MS_WORK_TENANT (organizations)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        s = settings()
        tenant = (s.ms_work_tenant or "organizations") if kind == "work" else (s.ms_tenant or "consumers")
        return cls(s.ms_client_id, s.ms_client_secret, tenant)

    @classmethod
    def for_account(cls, ms_tenant: str | None) -> "MsApp":
        """The authority an account was linked with; NULL (every account linked before migration 016, and personal
        accounts) means the personal default, EMAILD_MS_TENANT."""
        app = cls.from_settings()
        return replace(app, tenant=ms_tenant) if ms_tenant else app

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{quote(self.tenant, safe='')}/oauth2/v2.0"


def account_kind(ms_tenant: str | None) -> str:
    """'work' for an account linked against an organisational authority, else 'personal'."""
    t = (ms_tenant or "").strip().lower()
    return "work" if t and t not in PERSONAL_TENANTS else "personal"


# ---------- OAuth (authorisation code + PKCE) ----------

def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(app: MsApp, redirect_uri: str, state: str, challenge: str) -> str:
    q = {"client_id": app.client_id, "response_type": "code", "redirect_uri": redirect_uri,
         "response_mode": "query", "scope": " ".join(SCOPES), "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
    return f"{app.authority}/authorize?{urlencode(q)}"


def _creds_from(body: dict, previous: dict | None = None) -> dict:
    prev = previous or {}
    return {"access_token": body["access_token"],
            # rotation: use the new refresh token when one is returned, keep the old one otherwise
            "refresh_token": body.get("refresh_token") or prev.get("refresh_token"),
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
            "scope": body.get("scope") or prev.get("scope", "")}


def token_request(http: httpx.Client, app: MsApp, form: dict, previous: dict | None = None) -> dict:
    data = {"client_id": app.client_id, **form}
    if app.client_secret:
        data["client_secret"] = app.client_secret
    r = http.post(f"{app.authority}/token", data=data)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and body.get("access_token"):
        return _creds_from(body, previous)
    err = str(body.get("error") or f"http_{r.status_code}")
    # Microsoft's description starts with an AADSTS code that pinpoints the cause (e.g. AADSTS7000215 = wrong secret).
    # It never contains the secret; we keep only its first sentence.
    desc = first_sentence(body.get("error_description"), app.client_secret)
    if err in REAUTH_ERRORS:
        e = ReauthRequired(REAUTH_MESSAGE)
        e.error, e.description = err, desc       # for the link page's guidance (consent, conditional access...)
        raise e
    e2 = TokenError(f"Microsoft token endpoint refused the request: {err}" + (f" ({desc})" if desc else ""))
    e2.error, e2.description = err, desc
    raise e2


def first_sentence(description, secret: str = "") -> str:
    """The first sentence of an Entra error_description (it starts with the AADSTS code), at most 200 characters,
    with the client secret blanked out should it ever appear."""
    d = str(description or "").split("\r")[0].split("\n")[0].split(". ")[0][:200]
    return d.replace(secret, "***") if secret else d


_AADSTS = re.compile(r"AADSTS(\d{5,6})")
_ERROR_HELP = [
    (("65001", "90094", "90095", "65004"), "consent",
     "Your organisation needs to approve emAIl first",
     "Microsoft 365 asks an administrator to consent before an app can read mail. Ask your IT team to approve "
     "emAIl's app registration for Mail.Read, User.Read and offline_access (read-only), or to grant admin consent "
     "in the Entra admin centre. Then link the account again."),
    (("53000", "53001", "53002", "53003", "53004", "53009", "53010", "53011"), "conditional_access",
     "Blocked by your organisation's Conditional Access policy",
     "Your organisation only allows sign-ins from approved devices, apps or locations, and emAIl isn't one of "
     "them. Only your IT team can change this; some policies also force frequent re-sign-in, so a work account "
     "may need relinking more often."),
    (("50020", "700016", "50194", "50011", "700054", "900144"), "account_type",
     "This app registration doesn't accept this kind of account",
     "Set the app registration's Supported account types to 'Accounts in any organizational directory and "
     "personal Microsoft accounts' (see docs/SETUP.md, section 5b), check its redirect URI, and use the matching "
     "button: personal for Outlook.com/Hotmail, work or school for Microsoft 365."),
]


def explain_error(error: str | None, description: str | None = None) -> dict:
    """Friendly guidance for an Entra / Microsoft identity error: {"code": "AADSTS65001"|"", "kind", "title",
    "help", "detail"}. `detail` is the first sentence of Microsoft's description (never a secret)."""
    detail = first_sentence(description, settings().ms_client_secret)
    m = _AADSTS.search(detail or "") or _AADSTS.search(str(error or ""))
    num = m.group(1) if m else ""
    text = f"{error or ''} {detail}".lower()
    for codes, kind, title, help_ in _ERROR_HELP:
        if num in codes or (kind == "consent" and ("admin approval" in text or "consent_required" in text
                                                    or "admin consent" in text)) \
                or (kind == "conditional_access" and num.startswith("53")) \
                or (kind == "account_type" and ("account type" in text or "unauthorized_client" in text
                                                 and "tenant" in text)):
            return {"code": f"AADSTS{num}" if num else "", "kind": kind, "title": title, "help": help_,
                    "detail": detail}
    if error == "access_denied":
        return {"code": f"AADSTS{num}" if num else "", "kind": "denied", "title": "Sign-in was cancelled",
                "help": "Nothing was linked. Start again when you're ready.", "detail": detail}
    return {"code": f"AADSTS{num}" if num else "", "kind": "other", "title": "Microsoft sign-in failed",
            "help": "Start the link again; if it keeps failing, the code above tells your IT team (or Microsoft's "
                    "error lookup) what went wrong.", "detail": detail}


def exchange_code(app: MsApp, code: str, verifier: str, redirect_uri: str, http: httpx.Client | None = None) -> dict:
    with (http or httpx.Client(timeout=30)) as c:
        return token_request(c, app, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                      "code_verifier": verifier, "scope": " ".join(SCOPES)})


# ---------- mapping ----------

def labels_for(msg: dict, folder_names: dict[str, str]) -> list[str]:
    """Gmail-style labels from a Graph message (see module docstring). `folder_names`: folder id -> well-known name."""
    labels: list[str] = []
    folder = folder_names.get(msg.get("parentFolderId") or "")
    if folder in FOLDER_LABELS:
        labels.append(FOLDER_LABELS[folder])
    if msg.get("isRead") is False:
        labels.append("UNREAD")
    if ((msg.get("flag") or {}).get("flagStatus") or "").lower() == "flagged":
        labels.append("STARRED")
    if (msg.get("importance") or "").lower() == "high":
        labels.append("IMPORTANT")
    if (msg.get("inferenceClassification") or "").lower() == "other":
        labels.append("CATEGORY_OTHER")
    return labels


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _retry_after(r: httpx.Response, attempt: int) -> float:
    try:
        return max(0.0, float(r.headers.get("Retry-After", "")))
    except ValueError:
        return float(2 ** attempt)


def _error_code(r: httpx.Response) -> str:
    try:
        return str((r.json().get("error") or {}).get("code") or "")
    except (ValueError, AttributeError):
        return ""


# ---------- channel ----------

class OutlookChannel:
    provider = "outlook"

    def __init__(self, creds: dict, state: dict | None = None, *, app: MsApp | None = None,
                 budget: Budget | None = None, on_credentials: Callable[[dict], None] | None = None,
                 http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 tenant: str | None = None):
        self._creds = dict(creds)
        self._app = app or MsApp.from_settings()
        if tenant:          # the account's own authority (work or school accounts); refreshes must go there
            self._app = replace(self._app, tenant=tenant)
        self.budget = budget or Budget(10**9)
        self._on_credentials = on_credentials
        self._pending_creds: dict | None = None
        self._http = http or httpx.Client(timeout=60)
        self._sleep = sleep
        self.state: dict = json.loads(json.dumps(state or {}))  # deep copy; persisted by the caller
        self._meta: dict[str, dict] = {}  # message metadata seen in delta pages, saves a GET per fetch

    # --- auth ---

    def _refresh(self) -> None:
        if not self._creds.get("refresh_token"):
            raise ReauthRequired(REAUTH_MESSAGE)
        self.budget.charge()
        self._creds = token_request(self._http, self._app, {
            "grant_type": "refresh_token", "refresh_token": self._creds["refresh_token"],
            "scope": " ".join(SCOPES)}, previous=self._creds)
        if self._on_credentials:
            self._on_credentials(dict(self._creds))  # persist the rotated refresh token now, not at cycle end
        else:
            self._pending_creds = dict(self._creds)

    def _token(self) -> str:
        if not self._creds.get("access_token") or float(self._creds.get("expires_at") or 0) - 120 < time.time():
            self._refresh()
        return self._creds["access_token"]

    def updated_credentials(self) -> dict | None:
        out, self._pending_creds = self._pending_creds, None
        return out

    # --- http ---

    def _request(self, url: str, params: dict | None = None, prefer: str = "") -> httpx.Response:
        """GET with auth, immutable ids, throttling retries and budget accounting. Returns any non-throttled response."""
        reauthed = False
        attempt = 0
        while True:
            token = self._token()
            self.budget.charge()
            headers = {"Authorization": f"Bearer {token}", "Prefer": ", ".join(p for p in (IMMUTABLE, prefer) if p)}
            r = self._http.get(url, params=params, headers=headers)
            if r.status_code == 401 and not reauthed:
                reauthed = True
                self._refresh()  # access token revoked or clock skew: one forced refresh
                continue
            if r.status_code in RETRY_STATUSES:
                wait = _retry_after(r, attempt)
                if attempt >= MAX_RETRIES or wait > MAX_WAIT:
                    raise RateLimited(f"Microsoft Graph throttled (HTTP {r.status_code}, retry after {wait:.0f}s)")
                attempt += 1
                self._sleep(wait)
                continue
            return r

    def _get_json(self, url: str, params: dict | None = None, prefer: str = "") -> dict:
        r = self._request(url, params, prefer)
        r.raise_for_status()
        return r.json()

    # --- identity / folders ---

    def identity(self) -> tuple[str, str]:
        me = self._get_json(f"{GRAPH}/me", {"$select": "mail,userPrincipalName"})
        return (me.get("mail") or me.get("userPrincipalName") or "").lower(), ""

    def folders(self) -> list[str]:
        """Synced folders this mailbox has. Resolves well-known folder ids once (kept in state)."""
        known = self.state.get("folders")
        if known is None:
            known = {}
            for name in KNOWN_FOLDERS:
                r = self._request(f"{GRAPH}/me/mailFolders/{name}", {"$select": "id"})
                if r.status_code == 404:
                    if name not in OPTIONAL_FOLDERS and name != "deleteditems":
                        raise RuntimeError(f"Outlook folder {name!r} not found")
                    continue
                r.raise_for_status()
                known[name] = r.json()["id"]
            self.state["folders"] = known
        return [f for f in SYNC_FOLDERS if f in known]

    def _folder_names(self) -> dict[str, str]:
        return {fid: name for name, fid in (self.state.get("folders") or {}).items()}

    def labels_for(self, msg: dict) -> list[str]:
        return labels_for(msg, self._folder_names())

    # --- listing (verify) ---

    @staticmethod
    def _since(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def list_page(self, since_days: int, page_token: str | None) -> Page:
        """Ids received in the window, folder by folder. The token is {"f": folder index, "u": nextLink}."""
        folders = self.folders()
        tok = json.loads(page_token) if page_token else {"f": 0}
        if tok["f"] >= len(folders):
            return Page(ids=[], next_token=None)
        folder = folders[tok["f"]]
        if tok.get("u"):
            body = self._get_json(tok["u"])
        else:
            body = self._get_json(f"{GRAPH}/me/mailFolders/{folder}/messages", {
                "$filter": f"receivedDateTime ge {self._since(since_days)}", "$select": "id", "$top": str(PAGE_SIZE)})
        ids = [m["id"] for m in body.get("value", [])]
        if body.get("@odata.nextLink"):
            nxt = {"f": tok["f"], "u": body["@odata.nextLink"]}
        elif tok["f"] + 1 < len(folders):
            nxt = {"f": tok["f"] + 1}
        else:
            nxt = None
        return Page(ids=ids, next_token=json.dumps(nxt) if nxt else None)

    # --- delta ---

    def delta(self, folder: str, since_days: int) -> Changes:
        """One delta page for `folder`, from the saved link or a fresh window-filtered round.

        Returns Changes with added = present messages (new or changed; `labels` for each full record),
        deleted = `@removed` ids,
        next_token = nextLink, cursor = deltaLink. Call `advance` once the page is fully processed.
        """
        fs = (self.state.get("sync") or {}).get(folder) or {}
        url = fs.get("next") or fs.get("delta")
        params = None
        if not url:
            url = f"{GRAPH}/me/mailFolders/{folder}/messages/delta"
            params = {"$select": SELECT, "$filter": f"receivedDateTime ge {self._since(since_days)}"}
        r = self._request(url, params, prefer=f"odata.maxpagesize={PAGE_SIZE}")
        if r.status_code == 410 or _error_code(r) in EXPIRED_CODES:
            raise CursorExpired(f"{folder}: HTTP {r.status_code} {_error_code(r)}")
        r.raise_for_status()
        body = r.json()
        out = Changes(next_token=body.get("@odata.nextLink"), cursor=body.get("@odata.deltaLink"))
        for m in body.get("value", []):
            if "@removed" in m:
                out.deleted.append(m["id"])
                continue
            out.added.append(m["id"])
            if m.get("parentFolderId"):  # full record: label it and keep it for fetch()
                self._meta[m["id"]] = m
                out.labels[m["id"]] = self.labels_for(m)
            # else a partial change record: don't guess its labels; fetch() reads the full message if it's new
        return out

    def advance(self, folder: str, page: Changes) -> None:
        sync = self.state.setdefault("sync", {})
        sync[folder] = {"next": page.next_token} if page.next_token else {"delta": page.cursor}

    def reset_folder(self, folder: str) -> None:
        (self.state.get("sync") or {}).pop(folder, None)

    def caught_up(self) -> bool:
        """Every synced folder has finished its first (backfill) round."""
        sync = self.state.get("sync") or {}
        return all("delta" in (sync.get(f) or {}) for f in self.folders())

    def changes(self, cursor: str, page_token: str | None) -> Changes:  # Channel protocol; sync uses delta()
        raise NotImplementedError("Outlook syncs per folder via delta(); see sync._pull_outlook")

    # --- messages ---

    def _message(self, provider_id: str) -> dict | None:
        r = self._request(f"{GRAPH}/me/messages/{quote(provider_id, safe='')}", {"$select": SELECT})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def locate(self, provider_id: str) -> list[str] | None:
        """Current labels for a message that left a synced folder, or None if it no longer exists."""
        if not self.state.get("folders"):
            self.folders()
        m = self._message(provider_id)
        return None if m is None else self.labels_for(m)

    def fetch(self, provider_id: str) -> Item:
        meta = self._meta.pop(provider_id, None) or self._message(provider_id)
        if meta is None:
            raise LookupError(f"message {provider_id} no longer exists")
        r = self._request(f"{GRAPH}/me/messages/{quote(provider_id, safe='')}/$value")
        r.raise_for_status()
        raw = r.content
        item = parse_mime(raw)  # same path as Gmail: body, headers, List-*, Authentication-Results
        item.provider_id = provider_id
        item.provider_thread_id = meta.get("conversationId") or provider_id
        item.labels = self.labels_for(meta)
        item.snippet = (meta.get("bodyPreview") or "")[:1000]
        item.received_at = _ts(meta.get("receivedDateTime")) or item.sent_at
        item.size_bytes = len(raw)
        if not item.rfc_message_id:
            item.rfc_message_id = (meta.get("internetMessageId") or "")[:1000]
        return item
