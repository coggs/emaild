"""Opening one email from a list: a summary card (subject, sender, local date, content summary, key details) or the
whole email as clean plain text.

Key details are pulled deterministically from the email (no model call): dates/times mentioned, amounts with a
currency, https links (a count and the first few DOMAINS only - full links carry tracking ids and phishing bait), and
attachment names. The content summary is the triage decision's, unless it's weak (triage.weak_summary): then ONE
summarise call regenerates it and it is saved on the decision, so the next look is free.

Unsafe mail is never opened in full: spam, suspicious, one-time codes, security-held items, anything the provider
filed as spam/trash, and expired codes emAIl already scrubbed. Every full view is audited (`show_full`).
Everything here runs inside the caller's user session, so VPD decides what "your email" is: another user's item id
simply isn't found.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import store, triage
from .config import settings

log = logging.getLogger(__name__)

PART_CHARS = 3500            # one Telegram message of email text (measured after HTML escaping)
MAX_PARTS = 3                # more than this and the rest is "truncated - open on dashboard"
MCP_CHARS = 20000            # MCP clients get one long string
DETAIL_CAP = 5
LINK_DOMAINS = 3
UNSAFE_CATEGORIES = ("spam", "suspicious", "one_time")
UNSAFE_SOURCES = ("security", "one_time")

REFUSALS = {
    "spam": "This email was filed as spam, so emAIl won't show its text.",
    "suspicious": "This email was held as suspicious (possible phishing or impersonation), so emAIl won't show its "
                  "text. Check it in your mail provider if you're sure it's genuine.",
    "one_time": "This is a sign-in code or link; emAIl never forwards those in full.",
    "security": "This email was held by emAIl's security checks, so its text isn't shown.",
    "scrubbed": "This was a one-time code or sign-in link that has expired; emAIl removed its text.",
    "trash": "This email is in your mail provider's trash.",
    "minimal": "Your detail level here is minimal, so the bot doesn't send email text. /detail summary allows it.",
}


# ---------- loading ----------

def load(conn, item_id: int) -> dict | None:
    """The item with its triage decision (final category / action after corrections), or None if it isn't yours."""
    cur = conn.cursor()
    cur.execute("""SELECT i.id, i.subject, i.sender_name, i.sender_addr, i.received_at,
                          NVL(i.body_text, i.full_text), NVL(i.full_text, i.body_text), i.attachments, i.labels,
                          i.scrubbed_at, i.snippet, i.thread_id,
                          d.id, d.summary, NVL(JSON_VALUE(d.corrected, '$.category'), d.category), d.source,
                          NVL(JSON_VALUE(d.corrected, '$.action'), d.action)
                     FROM items i LEFT JOIN decisions d ON d.item_id = i.id
                    WHERE i.id = :id""", {"id": int(item_id)})
    r = cur.fetchone()
    if not r:
        return None
    return {"item_id": int(r[0]), "subject": r[1] or "(no subject)", "sender_name": r[2] or "",
            "sender_addr": r[3] or "", "received_at": r[4], "body": _text(r[5]), "full": _text(r[6]),
            "attachments": _attachment_names(r[7]), "labels": _labels(r[8]), "scrubbed": r[9] is not None,
            "snippet": r[10] or "", "thread_id": r[11], "decision_id": r[12], "summary": r[13] or "",
            "category": r[14], "source": r[15], "action": r[16]}


def owned(conn, item_id: int) -> bool:
    """Is this item id one of the user's emails? (VPD answers: another user's id finds nothing.) Callback data
    comes from the chat, so ids in it are re-checked before anything is read."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM items WHERE id = :id", {"id": int(item_id)})
    return cur.fetchone() is not None


def _text(v) -> str:
    if v is None:
        return ""
    return v.read() if hasattr(v, "read") else str(v)


def _attachment_names(v) -> list[str]:
    import json
    if isinstance(v, (str, bytes)):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return [str(a.get("filename") or "").strip()[:120] for a in (v or []) if isinstance(a, dict)
            and str(a.get("filename") or "").strip()]


def _labels(v) -> list[str]:
    import json
    if isinstance(v, (str, bytes)):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return [str(x) for x in (v or [])]


def refusal(it: dict) -> str | None:
    """Why this email must not be shown in full, or None. Mirrors search.EXCLUDE_UNSAFE (and is stricter: any
    spam/suspicious verdict or security hold refuses, whatever the action)."""
    if it.get("scrubbed"):
        return "scrubbed"
    labels = set(it.get("labels") or [])
    if "SPAM" in labels:
        return "spam"
    if "TRASH" in labels:
        return "trash"
    if it.get("category") in UNSAFE_CATEGORIES:
        return it["category"]
    if it.get("source") in UNSAFE_SOURCES:
        return "security" if it["source"] == "security" else "one_time"
    return None


# ---------- key details (deterministic) ----------

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DOW = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?"
_TIME = r"\d{1,2}(?::\d{2})?\s?(?:am|pm)|\d{1,2}:\d{2}"
_DATE_RES = [
    re.compile(rf"\b(?:{_DOW},?\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\.?(?:\s+\d{{4}})?(?:,?\s+(?:at\s+)?(?:{_TIME}))?",
               re.I),
    re.compile(rf"\b(?:{_DOW},?\s+)?{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?(?:,?\s+(?:at\s+)?(?:{_TIME}))?",
               re.I),
    re.compile(rf"\b\d{{4}}-\d{{2}}-\d{{2}}(?:[ T](?:{_TIME}))?\b", re.I),
    re.compile(rf"\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}(?:\s+(?:at\s+)?(?:{_TIME}))?", re.I),
    re.compile(rf"\b(?:today|tomorrow|tonight|(?:this|next)\s+(?:{_DOW}|morning|afternoon|evening|week))\b"
               rf"(?:\s+at\s+(?:{_TIME}))?", re.I),
    re.compile(rf"\b(?:{_DOW})\s+(?:at\s+)?(?:{_TIME})", re.I),
    re.compile(rf"\bat\s+(?:{_TIME})\b", re.I),
]
_CUR = r"(?:A\$|AU\$|AUD|US\$|USD|NZ\$|NZD|CA\$|CAD|EUR|GBP|JPY|\$|€|£|¥)"
_NUM = r"\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_AMOUNT = re.compile(rf"(?:{_CUR}\s?(?:{_NUM}))|(?:\b(?:{_NUM})\s?(?:AUD|USD|NZD|CAD|EUR|GBP|JPY)\b)")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _uniq(xs, cap: int) -> list[str]:
    out, seen = [], set()
    for x in xs:
        k = re.sub(r"\s+", " ", x).strip(" ,.").lower()
        if k and k not in seen and not any(k in s for s in seen):
            seen.add(k)
            out.append(re.sub(r"\s+", " ", x).strip(" ,."))
        if len(out) >= cap:
            break
    return out


def key_details(text: str, attachments: list[str] | None = None) -> dict:
    """{"dates", "amounts", "links": {"count", "domains"}, "attachments"} - regexes only, no model, no URLs."""
    text = text or ""
    no_urls = _URL.sub(" ", text)
    found = []
    for rx in _DATE_RES:
        found += [(m.start(), m.group(0)) for m in rx.finditer(no_urls)]
    found.sort()
    dates = []
    for _, s in found:
        s = s.strip()
        if len(s) < 3:
            continue
        if any(s.lower() in d.lower() for d in dates):
            continue
        dates = [d for d in dates if d.lower() not in s.lower()] + [s]
    amounts = [m.group(0) for m in _AMOUNT.finditer(no_urls)]
    https = [u for u in _URL.findall(text) if u.lower().startswith("https://")]
    domains = []
    for u in https:
        try:
            host = (urlsplit(u).hostname or "").lower()
        except ValueError:
            continue
        host = host[4:] if host.startswith("www.") else host
        if host and host not in domains:
            domains.append(host)
    return {"dates": _uniq(dates, DETAIL_CAP), "amounts": _uniq(amounts, DETAIL_CAP),
            "links": {"count": len(https), "domains": domains[:LINK_DOMAINS]},
            "attachments": list(dict.fromkeys(attachments or []))[:10]}


# ---------- the card ----------

def local_date(v, tz: str | None = None) -> str:
    """'Fri 09 Oct 2026 14:05' in EMAILD_TZ (stored times are UTC)."""
    if v is None:
        return ""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v[:16]
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(ZoneInfo(tz or settings().timezone)).strftime("%a %d %b %Y %H:%M")


def best_summary(conn, it: dict, router=None) -> str:
    """The decision's summary; when it's weak, one summarise() call (saved on the decision). Falls back to the
    snippet without a model."""
    s = (it.get("summary") or "").strip()
    if not triage.weak_summary(s, it.get("subject")):
        return s
    if router is not None and it.get("decision_id") and refusal(it) is None:
        item = triage.load_item(conn, it["item_id"])
        text = triage.summarise(router, item, conn) if item else None
        if text and not triage.weak_summary(text, it.get("subject")):
            conn.cursor().execute("UPDATE decisions SET summary = :s WHERE id = :id",
                                  {"s": text[:400], "id": int(it["decision_id"])})
            return text
    return s or (it.get("snippet") or "")[:300]


def card(conn, item_id: int, router=None) -> dict | None:
    """Summary-card fields for one email, or None when it isn't the user's. Unsafe mail gets a warning and no
    summary/details (nothing from a phishing email is repeated)."""
    it = load(conn, item_id)
    if it is None:
        return None
    why = refusal(it)
    sender = f"{it['sender_name']} <{it['sender_addr']}>" if it["sender_name"] else it["sender_addr"]
    out = {"item_id": it["item_id"], "subject": it["subject"], "from": sender, "date": local_date(it["received_at"]),
           "thread_id": it["thread_id"], "unsafe": why, "warning": REFUSALS.get(why) if why else None,
           "summary": "", "details": {"dates": [], "amounts": [], "links": {"count": 0, "domains": []},
                                      "attachments": it["attachments"]}}
    if why:
        return out
    out["summary"] = best_summary(conn, it, router)
    out["details"] = key_details(it["body"] or it["full"], it["attachments"])
    return out


def render_card(c: dict, item_url: str | None = None) -> str:
    """Telegram HTML for a card (everything escaped)."""
    e = html.escape
    out = [f"✉️ <b>{e(c['subject'])}</b>", f"from {e(c['from'])}", f"<i>{e(c['date'])}</i>"]
    if c.get("warning"):
        out.append(f"⚠️ {e(c['warning'])}")
        return "\n".join(out)
    if c.get("summary"):
        out.append("\n" + e(c["summary"][:600]))
    d = c["details"]
    if d["dates"]:
        out.append("🗓 " + e(" · ".join(d["dates"])))
    if d["amounts"]:
        out.append("💲 " + e(" · ".join(d["amounts"])))
    if d["links"]["count"]:
        n = d["links"]["count"]
        out.append(f"🔗 {n} link{'s' if n != 1 else ''}" + (f" ({e(', '.join(d['links']['domains']))})"
                                                            if d["links"]["domains"] else ""))
    if d["attachments"]:
        out.append("📎 " + e(", ".join(d["attachments"])))
    return "\n".join(out)[:3900]


def card_text(c: dict) -> str:
    """Plain-text card for the CLI."""
    out = [c["subject"], f"from {c['from']}", c["date"]]
    if c.get("warning"):
        return "\n".join(out + [f"! {c['warning']}"])
    if c.get("summary"):
        out += ["", c["summary"]]
    d = c["details"]
    if d["dates"]:
        out.append("dates: " + " · ".join(d["dates"]))
    if d["amounts"]:
        out.append("amounts: " + " · ".join(d["amounts"]))
    if d["links"]["count"]:
        out.append(f"links: {d['links']['count']}" + (f" ({', '.join(d['links']['domains'])})"
                                                      if d["links"]["domains"] else ""))
    if d["attachments"]:
        out.append("attachments: " + ", ".join(d["attachments"]))
    return "\n".join(out)


# ---------- the whole email ----------

_QUOTE_HEAD = re.compile(r"^\s*(?:On .{4,200}wrote:|-{2,}\s*Original Message\s*-{2,}|_{8,}|From: .+\s*$)", re.I)


def clean_text(text: str, strip_quotes: bool = True) -> str:
    """Plain text for reading: no quoted replies (lines starting with '>', or everything after "On ... wrote:"),
    runs of spaces collapsed, at most one blank line in a row. Not escaped - render escapes."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    lines = []
    for line in text.split("\n"):
        if strip_quotes and lines and _QUOTE_HEAD.match(line):
            break
        if strip_quotes and line.lstrip().startswith(">"):
            continue
        lines.append(re.sub(r"[ \t\f\v]+", " ", line).strip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


def chunk(text: str, size: int = PART_CHARS, max_parts: int = MAX_PARTS) -> tuple[list[str], bool]:
    """Split into at most `max_parts` raw-text parts whose HTML-ESCAPED length is <= size (so an entity is never cut
    and Telegram's 4096 limit holds), breaking at line ends where possible. Returns (parts, truncated)."""
    parts: list[str] = []
    cur = ""
    pieces = text.split("\n")
    i = 0
    while i < len(pieces):
        line = pieces[i]
        cand = f"{cur}\n{line}" if cur else line
        if len(html.escape(cand)) <= size:
            cur = cand
            i += 1
            continue
        if cur:
            parts.append(cur)
            cur = ""
        else:                                  # one line longer than a part: cut it by characters
            cut = size
            while len(html.escape(line[:cut])) > size:
                cut -= max(1, (len(html.escape(line[:cut])) - size))
            parts.append(line[:cut])
            pieces[i] = line[cut:]
        if len(parts) >= max_parts:
            return parts[:max_parts], True
    if cur:
        parts.append(cur)
    if len(parts) > max_parts:
        return parts[:max_parts], True
    return parts, False


def full(conn, item_id: int, actor: str, max_parts: int = MAX_PARTS, size: int = PART_CHARS) -> dict | None:
    """The whole email as clean text in parts, or {"refused": reason, "message"}. None when it isn't the user's.
    Records an audit entry (show_full) for every successful view."""
    it = load(conn, item_id)
    if it is None:
        return None
    why = refusal(it)
    if why:
        return {"item_id": it["item_id"], "subject": it["subject"], "refused": why, "message": REFUSALS[why]}
    text = clean_text(it["full"] or it["body"])
    parts, truncated = chunk(text, size, max_parts)
    store.audit(conn, actor, "show_full", str(it["item_id"]), {"parts": len(parts), "truncated": truncated})
    sender = f"{it['sender_name']} <{it['sender_addr']}>" if it["sender_name"] else it["sender_addr"]
    return {"item_id": it["item_id"], "subject": it["subject"], "from": sender, "date": local_date(it["received_at"]),
            "parts": parts, "truncated": truncated, "attachments": it["attachments"]}
