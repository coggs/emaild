"""Gmail connector: read-only (gmail.readonly), polling via the History API."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..models import Item
from ..normalise import parse_mime
from .base import Changes, CursorExpired, Page

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
RETRIES = 5  # googleapiclient retries 429/403-rateLimitExceeded/5xx with exponential backoff


class GmailChannel:
    provider = "gmail"

    def __init__(self, creds_info: dict):
        self._creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
        self._initial_token = self._creds.token
        if not self._creds.valid and self._creds.refresh_token:
            self._creds.refresh(Request())
        self._svc = build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    def updated_credentials(self) -> dict | None:
        if self._creds.token != self._initial_token:
            return json.loads(self._creds.to_json())
        return None

    def identity(self) -> tuple[str, str]:
        p = self._svc.users().getProfile(userId="me").execute(num_retries=RETRIES)
        return p["emailAddress"].lower(), str(p["historyId"])

    def list_page(self, since_days: int, page_token: str | None) -> Page:
        resp = self._svc.users().messages().list(
            userId="me", q=f"newer_than:{since_days}d", maxResults=200,
            pageToken=page_token or None, includeSpamTrash=False).execute(num_retries=RETRIES)
        return Page(ids=[m["id"] for m in resp.get("messages", [])], next_token=resp.get("nextPageToken"))

    def fetch(self, provider_id: str) -> Item:
        m = self._svc.users().messages().get(userId="me", id=provider_id, format="raw").execute(num_retries=RETRIES)
        raw = base64.urlsafe_b64decode(m["raw"].encode())
        item = parse_mime(raw)
        item.provider_id = m["id"]
        item.provider_thread_id = m["threadId"]
        item.labels = m.get("labelIds", [])
        item.snippet = (m.get("snippet") or "")[:1000]
        item.received_at = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
        item.size_bytes = int(m.get("sizeEstimate") or len(raw))
        return item

    def changes(self, cursor: str, page_token: str | None) -> Changes:
        try:
            resp = self._svc.users().history().list(
                userId="me", startHistoryId=cursor, pageToken=page_token or None, maxResults=500,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]).execute(num_retries=RETRIES)
        except HttpError as e:
            if e.resp.status == 404:
                raise CursorExpired(str(e)) from e
            raise
        out = Changes(next_token=resp.get("nextPageToken"), cursor=str(resp.get("historyId", cursor)))
        for h in resp.get("history", []):
            for a in h.get("messagesAdded", []):
                out.added.append(a["message"]["id"])
            for d in h.get("messagesDeleted", []):
                out.deleted.append(d["message"]["id"])
            for key in ("labelsAdded", "labelsRemoved"):
                for c in h.get(key, []):
                    msg = c["message"]
                    out.labels[msg["id"]] = msg.get("labelIds", [])
        return out
