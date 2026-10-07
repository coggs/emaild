"""Channel-agnostic record types. Email is the first channel; IM connectors will emit the same Item."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Address:
    addr: str
    name: str = ""


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size: int


@dataclass
class Item:
    provider_id: str
    provider_thread_id: str
    channel: str = "email"
    rfc_message_id: str = ""
    in_reply_to: str = ""
    sender: Address | None = None
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    subject: str = ""
    sent_at: datetime | None = None
    received_at: datetime | None = None
    snippet: str = ""
    body_text: str = ""        # cleaned
    full_text: str = ""        # decoded, uncleaned
    labels: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    size_bytes: int = 0
    raw: bytes = b""           # original MIME, stored encrypted in the blob store
