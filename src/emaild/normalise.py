"""MIME parsing, cleaning (quotes/signatures) and chunking for embedding."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup

from .models import Address, Attachment, Item

# ---------- parsing ----------

def _addresses(value: str | None) -> list[Address]:
    if not value:
        return []
    return [Address(addr=a.lower(), name=n) for n, a in getaddresses([str(value)]) if a]


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        block.insert_after("\n")
    text = soup.get_text()
    return _tidy(text)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            content = plain.get_content()
            if content and content.strip():
                return _tidy(content)
        except (LookupError, UnicodeDecodeError):
            pass
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        try:
            return html_to_text(html.get_content())
        except (LookupError, UnicodeDecodeError):
            pass
    return ""


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        out.append(Attachment(filename=part.get_filename() or "(unnamed)",
                              mime_type=part.get_content_type(), size=len(payload)))
    return out


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(str(value))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_mime(raw: bytes) -> Item:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
    full = _body_text(msg)
    senders = _addresses(msg.get("From"))
    meta = {}
    for header, key in (("List-Unsubscribe", "list_unsubscribe"), ("List-Unsubscribe-Post", "list_unsubscribe_post"),
                        ("List-Id", "list_id"), ("Precedence", "precedence"), ("Auto-Submitted", "auto_submitted"),
                        ("Reply-To", "reply_to")):
        if msg.get(header):
            meta[key] = str(msg.get(header))[:1000]
    auth = parse_auth_results(msg.get_all("Authentication-Results") or [])
    if auth:
        meta["auth"] = auth
    return Item(
        provider_id="", provider_thread_id="",
        rfc_message_id=str(msg.get("Message-ID", "")).strip()[:1000],
        in_reply_to=str(msg.get("In-Reply-To", "")).strip()[:1000],
        sender=senders[0] if senders else None,
        to=_addresses(msg.get("To")), cc=_addresses(msg.get("Cc")),
        subject=str(msg.get("Subject", "")).strip()[:1000],
        sent_at=_date(msg.get("Date")),
        full_text=full, body_text=clean_body(full),
        meta=meta, attachments=_attachments(msg), size_bytes=len(raw), raw=raw,
    )


_AUTH = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.I)


def parse_auth_results(headers: list) -> dict:
    """SPF/DKIM/DMARC verdicts from the receiving server's Authentication-Results header (first = most recent hop).

    Only the topmost header is trusted: it was added by the user's own provider (e.g. mx.google.com);
    lower ones could have been forged by the sender.
    """
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, val in _AUTH.findall(str(headers[0])):
        out.setdefault(key.lower(), val.lower())
    return out


# ---------- cleaning ----------

_REPLY_MARKERS = [
    re.compile(r"^On .{5,200}wrote:\s*$", re.M),                          # Gmail / Apple
    re.compile(r"^On .{5,200}\n.{0,200}wrote:\s*$", re.M),                # wrapped Gmail header
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^_{5,}\s*$\n^From: ", re.M),                              # Outlook separator
    re.compile(r"^From: .{1,200}\n(?:Sent|Date): .{1,200}\n(?:To|Subject): ", re.M),
]
_SIGNATURE = re.compile(r"^(-- ?|Sent from my \w+.*|Get Outlook for \w+.*)$", re.M)


def clean_body(text: str) -> str:
    """Keep only what this message adds: cut quoted history and trailing signatures."""
    if not text:
        return ""
    cut = len(text)
    for rx in _REPLY_MARKERS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    text = text[:cut]
    lines = [l for l in text.split("\n") if not l.lstrip().startswith(">")]
    text = "\n".join(lines)
    m = _SIGNATURE.search(text)
    if m and m.start() > len(text) * 0.3:  # don't treat an early "--" as a signature
        text = text[:m.start()]
    return _tidy(text)


# ---------- chunking ----------

def chunk_text(subject: str, body: str, max_words: int = 180, overlap: int = 30,
               max_bytes: int = 3800, max_chunks: int = 40) -> list[str]:
    """Word-window chunks, each prefixed with the subject for context.

    all-MiniLM-L12-v2 truncates input at 256 word-pieces, so ~180 words keeps most of each chunk visible.
    Chunks are capped to fit the VARCHAR2(4000) column.
    """
    prefix = f"Subject: {subject.strip()}\n" if subject.strip() else ""
    words = body.split()
    if not words:
        return [prefix.strip()] if prefix else []
    chunks, start = [], 0
    step = max(1, max_words - overlap)
    while start < len(words) and len(chunks) < max_chunks:
        piece = prefix + " ".join(words[start:start + max_words])
        enc = piece.encode("utf-8")
        if len(enc) > max_bytes:
            piece = enc[:max_bytes].decode("utf-8", errors="ignore")
        chunks.append(piece)
        if start + max_words >= len(words):
            break
        start += step
    return chunks
