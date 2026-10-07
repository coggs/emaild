"""Outlook.com connector (Microsoft Graph) against a fake HTTP transport - no network, no database."""
import base64
import json
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from emaild.channels import outlook
from emaild.channels.base import Budget, BudgetExhausted, RateLimited, ReauthRequired
from emaild.channels.outlook import MsApp, OutlookChannel, labels_for

G = "https://graph.microsoft.com/v1.0"
APP = MsApp("test-client-id", "test-secret", "consumers")
FOLDERS = {"inbox": "F-IN", "sentitems": "F-SENT", "junkemail": "F-JUNK", "deleteditems": "F-DEL"}  # no archive

MIME = (b"From: Alex Example <alex@example.org>\r\n"
        b"To: you@outlook.com\r\n"
        b"Subject: Weekly deals\r\n"
        b"Date: Tue, 06 Oct 2026 09:00:00 +0000\r\n"
        b"Message-ID: <deal-1@example.com>\r\n"
        b"List-Unsubscribe: <https://example.com/unsub?u=1>\r\n"
        b"Authentication-Results: mx.example.net; spf=pass smtp.mailfrom=example.com; dkim=pass header.d=example.com;"
        b" dmarc=pass header.from=example.com\r\n"
        b"Authentication-Results: forged.example.com; dmarc=fail\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Hello from example.com.\r\n")


def msg(mid, folder="F-IN", **kw):
    return {"id": mid, "conversationId": f"conv-{mid}", "internetMessageId": f"<{mid}@example.com>",
            "receivedDateTime": "2026-10-06T09:00:00Z", "isRead": True, "importance": "normal",
            "flag": {"flagStatus": "notFlagged"}, "inferenceClassification": "focused",
            "parentFolderId": folder, "bodyPreview": f"preview {mid}", **kw}


class FakeGraph:
    """Routes Graph + token requests. `delta[(folder, token)]` is a page body (or an int status);
    `script` holds one-shot responses keyed by path suffix, consumed before normal routing."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.delta: dict[tuple[str, str | None], dict | int] = {}
        self.messages: dict[str, dict] = {}
        self.script: dict[str, list[httpx.Response]] = {}
        self.token_response: tuple[int, dict] = (200, {"access_token": "at-new", "refresh_token": "rt-2",
                                                       "expires_in": 3600, "scope": "Mail.Read User.Read"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "login.microsoftonline.com":
            code, body = self.token_response
            return httpx.Response(code, json=body)
        path = url.path.removeprefix("/v1.0")
        for suffix, queue in self.script.items():
            if path.endswith(suffix) and queue:
                return queue.pop(0)
        q = parse_qs(url.query.decode())
        if path == "/me":
            return httpx.Response(200, json={"mail": None, "userPrincipalName": "You@Outlook.com"})
        if path.startswith("/me/mailFolders/") and path.count("/") == 3:
            name = path.rsplit("/", 1)[1]
            if name in FOLDERS:
                return httpx.Response(200, json={"id": FOLDERS[name]})
            return httpx.Response(404, json={"error": {"code": "ErrorFolderNotFound"}})
        if path.endswith("/messages/delta"):
            folder = path.split("/")[3]
            token = (q.get("$deltatoken") or q.get("$skiptoken") or [None])[0]
            page = self.delta.get((folder, token), {"value": [], "@odata.deltaLink": f"{G}/me/mailFolders/{folder}/messages/delta?$deltatoken={folder}-d0"})
            if isinstance(page, int):
                return httpx.Response(page, json={"error": {"code": "syncStateNotFound"}})
            return httpx.Response(200, json=page)
        if path.startswith("/me/messages/"):
            parts = path.split("/")
            mid = parts[3]
            if mid not in self.messages:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            if len(parts) == 5 and parts[4] == "$value":
                return httpx.Response(200, content=MIME, headers={"Content-Type": "message/rfc822"})
            return httpx.Response(200, json=self.messages[mid])
        return httpx.Response(500, json={"error": {"code": "unrouted", "message": str(url)}})

    def graph_paths(self):
        return [r.url.path.removeprefix("/v1.0") + ("?" + r.url.query.decode() if r.url.query else "")
                for r in self.requests if r.url.host == "graph.microsoft.com"]


def channel(fake, creds=None, state=None, budget=None, on_credentials=None, sleeps=None):
    creds = creds or {"access_token": "at", "refresh_token": "rt-1", "expires_at": time.time() + 3600}
    return OutlookChannel(creds, state, app=APP, budget=budget, on_credentials=on_credentials,
                          http=httpx.Client(transport=httpx.MockTransport(fake)),
                          sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# ---------- tokens ----------

def test_refresh_persists_rotated_refresh_token():
    fake, saved = FakeGraph(), []
    ch = channel(fake, creds={"access_token": "old", "refresh_token": "rt-1", "expires_at": time.time() - 10},
                 on_credentials=saved.append)
    assert ch.identity() == ("you@outlook.com", "")
    assert saved and saved[-1]["refresh_token"] == "rt-2" and saved[-1]["access_token"] == "at-new"
    form = parse_qs(fake.requests[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt-1"]
    assert "Mail.Read" in form["scope"][0] and "Mail.ReadWrite" not in form["scope"][0]
    graph = [r for r in fake.requests if r.url.host == "graph.microsoft.com"][0]
    assert graph.headers["Authorization"] == "Bearer at-new"
    assert 'IdType="ImmutableId"' in graph.headers["Prefer"]
    # the next refresh uses the rotated token; a response without one keeps the current one
    fake.token_response = (200, {"access_token": "at-3", "expires_in": 3600})
    ch._refresh()
    assert parse_qs(fake.requests[-1].content.decode())["refresh_token"] == ["rt-2"]
    assert saved[-1]["refresh_token"] == "rt-2" and saved[-1]["access_token"] == "at-3"


def test_invalid_grant_raises_reauth():
    fake = FakeGraph()
    fake.token_response = (400, {"error": "invalid_grant", "error_description": "AADSTS70000: expired"})
    ch = channel(fake, creds={"access_token": "", "refresh_token": "rt-1", "expires_at": 0})
    with pytest.raises(ReauthRequired, match="relink"):
        ch.identity()


def test_other_token_errors_do_not_echo_secret():
    fake = FakeGraph()
    fake.token_response = (401, {"error": "invalid_client"})
    with pytest.raises(outlook.TokenError) as e:
        channel(fake, creds={"refresh_token": "rt-1", "expires_at": 0}).identity()
    assert "invalid_client" in str(e.value) and "test-secret" not in str(e.value)


# ---------- listing / labels / MIME ----------

def test_list_page_follows_next_link_across_folders():
    fake = FakeGraph()
    fake.script["/me/mailFolders/inbox/messages"] = [
        httpx.Response(200, json={"value": [{"id": "a"}, {"id": "b"}],
                                  "@odata.nextLink": f"{G}/me/mailFolders/inbox/messages?$skip=50"}),
        httpx.Response(200, json={"value": [{"id": "c"}]})]
    fake.script["/me/mailFolders/sentitems/messages"] = [httpx.Response(200, json={"value": [{"id": "s1"}]})]
    fake.script["/me/mailFolders/junkemail/messages"] = [httpx.Response(200, json={"value": [{"id": "j1"}]})]
    ch = channel(fake)
    ids, token = [], None
    while True:
        page = ch.list_page(30, token)
        ids += page.ids
        token = page.next_token
        if not token:
            break
    assert ids == ["a", "b", "c", "s1", "j1"]
    first = [p for p in fake.graph_paths() if p.startswith("/me/mailFolders/inbox/messages?")][0]
    q = parse_qs(urlparse(first).query)
    assert q["$filter"][0].startswith("receivedDateTime ge ") and q["$top"] == ["50"] and q["$select"] == ["id"]
    assert any(parse_qs(urlparse(p).query).get("$skip") == ["50"] for p in fake.graph_paths())  # nextLink followed


@pytest.mark.parametrize("fields, expected", [
    ({"parentFolderId": "F-JUNK"}, ["SPAM"]),
    ({"parentFolderId": "F-SENT"}, ["SENT"]),
    ({"parentFolderId": "F-IN"}, ["INBOX"]),
    ({"parentFolderId": "F-DEL"}, ["TRASH"]),
    ({"parentFolderId": "F-ARCH"}, []),
    ({"flag": {"flagStatus": "flagged"}}, ["INBOX", "STARRED"]),
    ({"isRead": False}, ["INBOX", "UNREAD"]),
    ({"importance": "high"}, ["INBOX", "IMPORTANT"]),
    ({"inferenceClassification": "other"}, ["INBOX", "CATEGORY_OTHER"]),
])
def test_label_mapping(fields, expected):
    names = {v: k for k, v in FOLDERS.items()} | {"F-ARCH": "archive"}
    assert labels_for(msg("m", **fields), names) == expected


def test_fetch_goes_through_normalise():
    fake = FakeGraph()
    fake.messages["m1"] = msg("m1", isRead=False, inferenceClassification="other")
    ch = channel(fake, state={"folders": FOLDERS})
    item = ch.fetch("m1")
    assert item.provider_id == "m1" and item.provider_thread_id == "conv-m1"
    assert item.rfc_message_id == "<deal-1@example.com>"
    assert item.meta["list_unsubscribe"] == "<https://example.com/unsub?u=1>"
    assert item.meta["auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}  # topmost header only
    assert item.sender.addr == "alex@example.org" and item.subject == "Weekly deals"
    assert item.labels == ["INBOX", "UNREAD", "CATEGORY_OTHER"]
    assert item.raw == MIME and item.snippet == "preview m1" and item.received_at.year == 2026
    assert any(p.endswith("/me/messages/m1/$value") for p in fake.graph_paths())


# ---------- throttling and budget ----------

def test_429_retry_after_respected_and_budget_counts_every_attempt():
    fake, sleeps = FakeGraph(), []
    fake.script["/me"] = [httpx.Response(429, headers={"Retry-After": "7"}, json={}),
                          httpx.Response(503, headers={"Retry-After": "2"}, json={})]
    budget = Budget(10)
    ch = channel(fake, budget=budget, sleeps=sleeps)
    assert ch.identity()[0] == "you@outlook.com"
    assert sleeps == [7.0, 2.0]
    assert budget.left == 7  # three attempts charged


def test_throttling_beyond_limits_stops_the_cycle():
    fake = FakeGraph()
    fake.script["/me"] = [httpx.Response(429, headers={"Retry-After": "3600"}, json={})]
    with pytest.raises(RateLimited):
        channel(fake).identity()
    fake.script["/me"] = [httpx.Response(429, headers={"Retry-After": "1"}, json={}) for _ in range(10)]
    with pytest.raises(RateLimited):
        channel(fake).identity()


def test_budget_exhausted():
    with pytest.raises(BudgetExhausted):
        channel(FakeGraph(), budget=Budget(0)).identity()


# ---------- sync driver (fake store) ----------

class FakeConn:
    def commit(self):
        pass

    def rollback(self):
        pass


class FakeStore:
    def __init__(self, account):
        self.account = account
        self.items: dict[str, list[str]] = {}
        self.deleted: list[str] = []
        self.label_updates: list[tuple[str, list[str]]] = []
        self.failures: dict[str, str] = {}

    def install(self, monkeypatch, sync):
        st = sync.store
        monkeypatch.setattr(st, "get_account", lambda conn, aid: dict(self.account))
        monkeypatch.setattr(st, "update_account", self.update_account)
        monkeypatch.setattr(st, "existing_ids", lambda conn, aid, ids: {i for i in ids if i in self.items})
        monkeypatch.setattr(st, "item_exists", lambda conn, aid, pid: pid in self.items)
        monkeypatch.setattr(st, "insert_item", self.insert_item)
        monkeypatch.setattr(st, "update_labels", self.update_labels)
        monkeypatch.setattr(st, "mark_deleted", lambda conn, aid, pid: self.deleted.append(pid))
        monkeypatch.setattr(st, "record_failure", lambda conn, aid, pid, err: self.failures.__setitem__(pid, err))
        monkeypatch.setattr(st, "clear_failure", lambda conn, aid, pid: self.failures.pop(pid, None))
        monkeypatch.setattr(st, "retryable_failures", lambda conn, aid: [])

        @contextmanager
        def session(ctx):
            yield FakeConn()
        monkeypatch.setattr(sync.db, "user_session", session)

    def update_account(self, conn, aid, **fields):
        if "sync_state" in fields:
            fields["sync_state"] = json.loads(json.dumps(fields["sync_state"]))  # as the DB would round-trip it
        self.account.update(fields)

    def insert_item(self, conn, ctx, aid, address, item):
        self.items[item.provider_id] = list(item.labels)
        return len(self.items)

    def update_labels(self, conn, aid, pid, labels):
        self.items[pid] = list(labels)
        self.label_updates.append((pid, list(labels)))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EMAILD_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("EMAILD_MS_CLIENT_ID", "test-client-id")
    from emaild import config, crypto, sync
    from emaild.db import UserCtx
    config.settings.cache_clear()
    ctx = UserCtx(1, 1, "you@example.com")
    creds = {"access_token": "at", "refresh_token": "rt-1", "expires_at": time.time() + 3600}
    token_enc = crypto.encrypt(config.settings().master_key, 1, 1, json.dumps(creds).encode())
    account = {"id": 7, "provider": "outlook", "address": "you@outlook.com", "status": "active",
               "token_enc": token_enc, "sync_cursor": None, "backfill_token": None, "backfill_done": False,
               "sync_state": None}
    fs, fake = FakeStore(account), FakeGraph()
    fs.install(monkeypatch, sync)
    real = sync.OutlookChannel
    monkeypatch.setattr(sync, "OutlookChannel", lambda creds, state, **kw: real(
        creds, state, app=APP, http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None, **kw))
    return sync, ctx, fs, fake


def _delta_link(folder, tok):
    return f"{G}/me/mailFolders/{folder}/messages/delta?$deltatoken={tok}"


def test_backfill_then_incremental_uses_saved_delta_links(env):
    sync, ctx, fs, fake = env
    for mid, folder in (("m1", "F-IN"), ("m2", "F-IN"), ("s1", "F-SENT"), ("j1", "F-JUNK")):
        fake.messages[mid] = msg(mid, folder)
    fake.delta[("inbox", None)] = {"value": [fake.messages["m1"]],
                                   "@odata.nextLink": f"{G}/me/mailFolders/inbox/messages/delta?$skiptoken=p2"}
    fake.delta[("inbox", "p2")] = {"value": [fake.messages["m2"]], "@odata.deltaLink": _delta_link("inbox", "in-d1")}
    fake.delta[("sentitems", None)] = {"value": [fake.messages["s1"]], "@odata.deltaLink": _delta_link("sentitems", "se-d1")}
    fake.delta[("junkemail", None)] = {"value": [fake.messages["j1"]], "@odata.deltaLink": _delta_link("junkemail", "ju-d1")}

    stats = sync.sync_account(ctx, 7)
    assert stats["stored"] == 4 and stats["phase"] == "backfill"
    assert fs.items == {"m1": ["INBOX"], "m2": ["INBOX"], "s1": ["SENT"], "j1": ["SPAM"]}
    state = fs.account["sync_state"]
    assert state["sync"] == {"inbox": {"delta": _delta_link("inbox", "in-d1")},
                             "sentitems": {"delta": _delta_link("sentitems", "se-d1")},
                             "junkemail": {"delta": _delta_link("junkemail", "ju-d1")}}
    assert "archive" not in state["folders"] and state["folders"]["deleteditems"] == "F-DEL"  # 404 archive skipped
    assert fs.account["backfill_done"] is True and fs.account["status"] == "active"
    first_delta = [p for p in fake.graph_paths() if p.startswith("/me/mailFolders/inbox/messages/delta")][0]
    assert "receivedDateTime ge" in parse_qs(urlparse(first_delta).query)["$filter"][0]
    delta_req = [r for r in fake.requests if r.url.path.endswith("/delta")][0]
    assert "odata.maxpagesize=50" in delta_req.headers["Prefer"]
    # metadata came with the delta page: only $value per new message, no extra GET
    assert not [p for p in fake.graph_paths() if p.startswith("/me/messages/") and not p.endswith("$value")]

    # second cycle: new mail, a flag change, a move into Junk, and a deletion
    fs.account["backfill_done"] = True
    fake.requests.clear()
    fake.messages["m3"] = msg("m3", "F-IN", isRead=False)
    fake.messages["m1"] = msg("m1", "F-IN", flag={"flagStatus": "flagged"})
    fake.messages["m2"] = msg("m2", "F-JUNK")
    del fake.messages["s1"]
    fake.delta[("inbox", "in-d1")] = {"value": [fake.messages["m3"], fake.messages["m1"],
                                                {"id": "m2", "@removed": {"reason": "deleted"}},
                                                {"id": "never-stored", "@removed": {"reason": "deleted"}}],
                                      "@odata.deltaLink": _delta_link("inbox", "in-d2")}
    fake.delta[("sentitems", "se-d1")] = {"value": [{"id": "s1", "@removed": {"reason": "deleted"}}],
                                          "@odata.deltaLink": _delta_link("sentitems", "se-d2")}
    fake.delta[("junkemail", "ju-d1")] = {"value": [fake.messages["m2"]], "@odata.deltaLink": _delta_link("junkemail", "ju-d2")}
    stats = sync.sync_account(ctx, 7)
    paths = fake.graph_paths()
    assert any(parse_qs(urlparse(p).query).get("$deltatoken") == ["in-d1"] for p in paths)
    assert not any("$filter" in parse_qs(urlparse(p).query) for p in paths)
    assert stats["stored"] == 1 and fs.items["m3"] == ["INBOX", "UNREAD"]
    assert fs.items["m1"] == ["INBOX", "STARRED"]
    assert fs.items["m2"] == ["SPAM"]  # move into Junk -> update_labels path (reclassifies as spam)
    assert fs.deleted == ["s1"]  # 404 on lookup = really deleted; never-stored ids are ignored
    assert fs.account["sync_state"]["sync"]["inbox"] == {"delta": _delta_link("inbox", "in-d2")}


def test_expired_delta_token_falls_back_to_bounded_rebackfill(env):
    sync, ctx, fs, fake = env
    fs.account.update(backfill_done=True, sync_state={
        "folders": FOLDERS, "sync": {"inbox": {"delta": _delta_link("inbox", "stale")},
                                     "sentitems": {"delta": _delta_link("sentitems", "se")},
                                     "junkemail": {"delta": _delta_link("junkemail", "ju")}}})
    fake.messages["m9"] = msg("m9")
    fake.delta[("inbox", "stale")] = 410
    fake.delta[("inbox", None)] = {"value": [fake.messages["m9"]], "@odata.deltaLink": _delta_link("inbox", "fresh")}
    stats = sync.sync_account(ctx, 7)
    assert stats["resynced"] == 1 and stats["stored"] == 1
    refetch = [p for p in fake.graph_paths() if p.startswith("/me/mailFolders/inbox/messages/delta?")
               and "$filter" in parse_qs(urlparse(p).query)]
    assert len(refetch) == 1 and "receivedDateTime ge" in parse_qs(urlparse(refetch[0]).query)["$filter"][0]
    assert fs.account["sync_state"]["sync"]["inbox"] == {"delta": _delta_link("inbox", "fresh")}
    assert fs.account["status"] == "active"


def test_budget_stop_keeps_page_link_and_resumes(env, monkeypatch):
    sync, ctx, fs, fake = env
    monkeypatch.setattr(sync, "MAX_PER_RUN", 5)  # 4 folder lookups + 1 delta page, then no budget for $value
    for mid in ("m1", "m2"):
        fake.messages[mid] = msg(mid)
    fake.delta[("inbox", None)] = {"value": [fake.messages["m1"], fake.messages["m2"]],
                                   "@odata.deltaLink": _delta_link("inbox", "d1")}
    stats = sync.sync_account(ctx, 7)
    assert stats.get("budget_exhausted") and stats["stored"] == 0 and not fs.failures
    assert "inbox" not in (fs.account["sync_state"] or {}).get("sync", {})  # not advanced past an unfinished page
    assert fs.account["status"] == "active"
    monkeypatch.setattr(sync, "MAX_PER_RUN", 400)
    stats = sync.sync_account(ctx, 7)
    assert set(fs.items) == {"m1", "m2"} and fs.account["backfill_done"] is True


def test_invalid_grant_marks_account_reauth(env):
    sync, ctx, fs, fake = env
    from emaild import config, crypto
    fs.account["token_enc"] = crypto.encrypt(config.settings().master_key, 1, 1, json.dumps(
        {"access_token": "", "refresh_token": "rt-dead", "expires_at": 0}).encode())
    fake.token_response = (400, {"error": "invalid_grant"})
    sync.sync_account(ctx, 7)
    assert fs.account["status"] == "reauth"
    assert fs.account["last_error"] == "Microsoft sign-in expired — relink the account"


def test_rotated_token_saved_during_sync(env):
    sync, ctx, fs, fake = env
    from emaild import config, crypto
    fs.account["token_enc"] = crypto.encrypt(config.settings().master_key, 1, 1, json.dumps(
        {"access_token": "", "refresh_token": "rt-1", "expires_at": 0}).encode())
    sync.sync_account(ctx, 7)
    saved = json.loads(crypto.decrypt(config.settings().master_key, 1, 1, fs.account["token_enc"]))
    assert saved["refresh_token"] == "rt-2"


# ---------- web OAuth ----------

@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("EMAILD_MS_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("EMAILD_MS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("EMAILD_PUBLIC_URL", "https://emaild.example.com")
    monkeypatch.setenv("EMAILD_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.delenv("EMAILD_WEB_PASSWORD", raising=False)
    from emaild import config
    config.settings.cache_clear()
    from fastapi.testclient import TestClient

    from emaild.db import UserCtx
    from emaild.web import app as webapp
    monkeypatch.setattr(webapp.users, "resolve", lambda email=None: UserCtx(1, 1, "you@example.com"))
    return webapp, TestClient(webapp.app, follow_redirects=False)


def test_oauth_start_builds_authorize_url_with_pkce_and_state(web):
    webapp, client = web
    r = client.get("/oauth/microsoft/start")
    assert r.status_code in (302, 307)
    u = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "login.microsoftonline.com" and u.path == "/consumers/oauth2/v2.0/authorize"
    assert q["client_id"] == "test-client-id" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://emaild.example.com/oauth/microsoft/callback"
    assert q["scope"].split() == ["offline_access", "User.Read", "Mail.Read"]
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    verifier = webapp._ms_pending[q["state"]][1]
    import hashlib
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert "test-secret" not in r.headers["location"]


def test_oauth_callback_exchanges_code_and_stores_outlook_account(web, monkeypatch):
    webapp, client = web
    fake = FakeGraph()
    real_exchange = outlook.exchange_code
    monkeypatch.setattr(webapp.outlook, "exchange_code", lambda app, code, verifier, redirect: real_exchange(
        app, code, verifier, redirect, http=httpx.Client(transport=httpx.MockTransport(fake))))
    real_channel = outlook.OutlookChannel
    monkeypatch.setattr(webapp.outlook, "OutlookChannel", lambda creds, app=None: real_channel(
        creds, app=app, http=httpx.Client(transport=httpx.MockTransport(fake))))
    linked = {}

    @contextmanager
    def session(ctx):
        yield FakeConn()
    monkeypatch.setattr(webapp.db, "user_session", session)
    monkeypatch.setattr(webapp.store, "upsert_account",
                        lambda conn, provider, address, token_enc: linked.update(provider=provider, address=address,
                                                                                 token=token_enc) or 42)
    monkeypatch.setattr(webapp.store, "audit", lambda *a, **k: None)

    state = parse_qs(urlparse(client.get("/oauth/microsoft/start").headers["location"]).query)["state"][0]
    verifier = webapp._ms_pending[state][1]
    r = client.get(f"/oauth/microsoft/callback?state={state}&code=auth-code-1")
    assert r.status_code == 303 and linked["provider"] == "outlook" and linked["address"] == "you@outlook.com"
    form = parse_qs(fake.requests[0].content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["auth-code-1"]
    assert form["code_verifier"] == [verifier] and form["client_secret"] == ["test-secret"]
    assert form["redirect_uri"] == ["https://emaild.example.com/oauth/microsoft/callback"]
    from emaild import config, crypto
    creds = json.loads(crypto.decrypt(config.settings().master_key, 1, 1, linked["token"]))
    assert creds["refresh_token"] == "rt-2"
    # state is single-use
    assert client.get(f"/oauth/microsoft/callback?state={state}&code=auth-code-1").status_code == 400
