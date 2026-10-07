"""Recommendations: lists you could unsubscribe from, and sent emails still waiting on someone else.

Unsubscribing is always a user action - never automatic - and emAIl stays read-only towards Gmail:
- one-click (RFC 8058) is an HTTPS POST of `List-Unsubscribe=One-Click` to the sender's own endpoint. It's the
  only thing emAIl sends anywhere, and only to public hosts (an email can't make us POST into the LAN);
- a plain unsubscribe URL is handed to the user to open themselves (a GET on it may be a tracking or "are you
  sure?" page, so we never fetch it);
- a mailto: unsubscribe is handed to the user too (emAIl never sends email).
Senders ever flagged spam/suspicious (or in Gmail's Spam) are never suggested and can't be acted on: unsubscribing
from spam just confirms the address is live.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx
import oracledb

from . import identities, store, triage
from .brief import FINAL_ACTION
from .search import EXCLUDE_UNSAFE

METHODS = ("one_click", "url", "mailto")
_ANGLE = re.compile(r"<\s*([^>]+?)\s*>")
TIMEOUT = 15.0
USER_AGENT = "emAIl/0.1 (RFC 8058 one-click unsubscribe)"


# ---------- List-Unsubscribe parsing (pure) ----------

def parse_list_unsubscribe(header: str | None, post_header: str | None = None) -> dict:
    """RFC 2369 `List-Unsubscribe: <https://...>, <mailto:...>` -> the best method we can offer.

    one_click needs an https URL *and* `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058); otherwise
    an https URL is for the user to open (url), then mailto, then a plain-http URL as a last resort (url).
    Returns {"method", "target", "https", "mailto"}; method/target are None when nothing usable is there.
    """
    out = {"method": None, "target": None, "https": [], "mailto": []}
    if not header:
        return out
    raw = _ANGLE.findall(header) or [p.strip() for p in header.split(",")]
    http = []
    for t in raw:
        t = re.sub(r"\s+", "", t)            # folded headers can leave whitespace inside the URL
        low = t.lower()
        if low.startswith("https://") and urlsplit(t).hostname:
            out["https"].append(t)
        elif low.startswith("mailto:") and "@" in t:
            out["mailto"].append(t)
        elif low.startswith("http://") and urlsplit(t).hostname:
            http.append(t)
    one_click = bool(post_header and "list-unsubscribe=one-click" in re.sub(r"\s+", "", post_header.lower()))
    if out["https"]:
        out["method"], out["target"] = ("one_click" if one_click else "url"), out["https"][0]
    elif out["mailto"]:
        out["method"], out["target"] = "mailto", out["mailto"][0]
    elif http:
        out["method"], out["target"] = "url", http[0]
    if out["target"]:
        out["target"] = out["target"][:2000]
    return out


# ---------- one-click POST ----------

def _resolve(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def public_host(host: str | None) -> bool:
    """Only POST to hosts on the public internet: an unsubscribe header is attacker-controlled text, and this
    runs inside the homelab, so it mustn't be able to reach routers, NAS or other LAN services."""
    if not host or host.lower() in ("localhost",) or host.lower().endswith((".local", ".lan", ".internal", ".home.arpa")):
        return False
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a.split("%")[0]) for a in _resolve(host)]
        except (OSError, ValueError):
            return False
    return bool(addrs) and all(a.is_global for a in addrs)


def _post(url: str) -> tuple[int, str | None]:
    """One HTTPS POST, no redirects followed, no cookies. Returns (status, Location header). Monkeypatched in tests."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as c:
        r = c.post(url, content=b"List-Unsubscribe=One-Click",
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        return r.status_code, r.headers.get("location")


def one_click_unsubscribe(target_url: str, max_redirects: int = 3) -> tuple[bool, str]:
    """RFC 8058 one-click: POST `List-Unsubscribe=One-Click`. Never GET, never http://, never mailto:.
    Redirects are followed (still as POST) only to other public https URLs."""
    url = target_url
    for _ in range(max_redirects + 1):
        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            return False, f"refused: only https one-click URLs are used (got {parts.scheme or 'none'}:)"
        if not public_host(parts.hostname):
            return False, "refused: unsubscribe host is not a public internet address"
        try:
            status, location = _post(url)
        except httpx.HTTPError as e:
            return False, f"request failed: {type(e).__name__}"
        if 200 <= status < 300:
            return True, f"unsubscribed (HTTP {status})"
        if status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        return False, f"sender's server answered HTTP {status}"
    return False, "too many redirects"


# ---------- unsubscribe suggestions ----------

_CANDIDATES_SQL = f"""
WITH m AS (
    SELECT LOWER(i.sender_addr) addr, i.sender_name, i.received_at,
           JSON_VALUE(i.meta, '$.list_unsubscribe' RETURNING VARCHAR2(4000)) lu,
           JSON_VALUE(i.meta, '$.list_unsubscribe_post' RETURNING VARCHAR2(4000)) lup,
           JSON_VALUE(i.meta, '$.list_id' RETURNING VARCHAR2(4000)) lid,
           {FINAL_ACTION} act
      FROM items i LEFT JOIN decisions d ON d.item_id = i.id
     WHERE i.is_from_me = FALSE AND i.sender_addr IS NOT NULL AND i.received_at >= :since
       AND JSON_EXISTS(i.meta, '$.list_unsubscribe')
       AND {EXCLUDE_UNSAFE}
), agg AS (
    SELECT addr, MAX(sender_name) sender_name, COUNT(*) n,
           SUM(CASE WHEN act = 'archive' THEN 1 ELSE 0 END) archived,
           SUM(CASE WHEN act IN ('keep', 'alert') THEN 1 ELSE 0 END) kept,
           MAX(received_at) last_received,
           MAX(lu) KEEP (DENSE_RANK LAST ORDER BY received_at) lu,
           MAX(lup) KEEP (DENSE_RANK LAST ORDER BY received_at) lup,
           MAX(lid) KEEP (DENSE_RANK LAST ORDER BY received_at) lid
      FROM m GROUP BY addr HAVING COUNT(*) >= :min_count
)
SELECT a.addr, a.sender_name, a.n, a.archived, a.kept, a.last_received, a.lu, a.lup, a.lid, u.id, u.status, u.detail
  FROM agg a
  LEFT JOIN sender_stats ss ON ss.sender_addr = a.addr
  LEFT JOIN unsubscribes u ON u.sender_addr = a.addr
 WHERE NVL(ss.replied, 0) = 0 AND NVL(ss.sent_to, 0) = 0
   AND (a.archived >= 0.6 * a.n OR (a.kept = 0 AND a.n >= 3 * :min_count))
   AND NVL(u.status, 'suggested') NOT IN ('done', 'dismissed')
   AND NOT (NVL(u.status, 'suggested') = 'manual' AND u.acted_at > SYSTIMESTAMP - INTERVAL '30' DAY)
   AND NOT EXISTS (SELECT 1 FROM items x LEFT JOIN decisions dx ON dx.item_id = x.id
                    WHERE LOWER(x.sender_addr) = a.addr
                      AND (NVL(JSON_SERIALIZE(x.labels), '[]') LIKE '%"SPAM"%'
                           OR NVL(JSON_VALUE(dx.corrected, '$.category'), dx.category) IN ('spam', 'suspicious')))
 ORDER BY a.archived DESC, a.n DESC
 FETCH FIRST 100 ROWS ONLY"""


def score(n: int, archived: int) -> float:
    """More mail and more of it filed as noise = a better suggestion."""
    return round(n * (0.5 + archived / max(n, 1)), 2)


def unsubscribe_candidates(conn: oracledb.Connection, days: int = 60, min_count: int = 3, limit: int = 20) -> list[dict]:
    """List mail you never engage with: you've never replied or written to the sender, you've never confirmed
    keep/alert for them, and most of it is filed as noise (or there's a lot of it and none is kept).
    'manual' suggestions (link handed over) rest for 30 days; done/dismissed never come back."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    cur = conn.cursor()
    cur.execute(_CANDIDATES_SQL, {"since": since, "min_count": int(min_count)})
    rows = cur.fetchall()
    protected = identities.list_all(conn)
    out = []
    for addr, name, n, archived, kept, last, lu, lup, lid, uid, status, detail in rows:
        if any(identities.is_allowed(addr, p["allowed"]) for p in protected):
            continue                                   # clubs/people you protect matter to you
        if triage.sender_keeps(conn, addr):
            continue                                   # you've confirmed keep/alert for this sender before
        how = parse_list_unsubscribe(lu, lup)
        if not how["method"] or len((how["target"] or "").encode()) > 4000:
            continue                                   # no usable method, or a link too long to store
        share = archived / n if n else 0.0
        why = f"{n} emails in {days} days, {share:.0%} filed as noise, you've never replied or written to them"
        out.append({"id": uid, "sender_addr": addr, "sender_name": name or addr, "count": int(n),
                    "archived_share": round(share, 2), "last_received": str(last)[:16] if last else "",
                    "list_id": lid, "method": how["method"], "target": how["target"], "reason": why,
                    "status": status or "suggested", "detail": detail, "score": score(int(n), int(archived))})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def _ensure_row(cur, c: dict) -> int:
    """Record a suggestion so surfaces can refer to it by a short id (e.g. Telegram's 64-byte callback data)."""
    if c.get("id"):
        cur.execute("""UPDATE unsubscribes SET method = :method, target = :target, list_id = :list_id
                        WHERE id = :id AND status IN ('suggested', 'failed', 'manual')""",
                    {"method": c["method"], "target": c["target"], "list_id": (c.get("list_id") or "")[:1000] or None,
                     "id": c["id"]})
        return int(c["id"])
    out = cur.var(oracledb.NUMBER)
    cur.execute("""INSERT INTO unsubscribes (sender_addr, list_id, method, target, status)
                   VALUES (:addr, :list_id, :method, :target, 'suggested') RETURNING id INTO :out""",
                {"addr": c["sender_addr"], "list_id": (c.get("list_id") or "")[:1000] or None,
                 "method": c["method"], "target": c["target"], "out": out})
    return int(out.getvalue()[0])


def suggestions(conn: oracledb.Connection, limit: int = 20) -> list[dict]:
    """unsubscribe_candidates, each with a stored row id (inserted as 'suggested' if new)."""
    cands = unsubscribe_candidates(conn, limit=limit)
    cur = conn.cursor()
    for c in cands:
        c["id"] = _ensure_row(cur, c)
    return cands


def _sender_flagged(cur, addr: str) -> bool:
    cur.execute("""SELECT COUNT(*) FROM items x LEFT JOIN decisions dx ON dx.item_id = x.id
                    WHERE LOWER(x.sender_addr) = :addr
                      AND (NVL(JSON_SERIALIZE(x.labels), '[]') LIKE '%"SPAM"%'
                           OR NVL(JSON_VALUE(dx.corrected, '$.category'), dx.category) IN ('spam', 'suspicious'))""",
                {"addr": addr})
    return cur.fetchone()[0] > 0


def _latest_header(cur, addr: str) -> dict | None:
    """The newest safe list email from this sender - its header is the one to use (older ones may be stale)."""
    cur.execute(f"""SELECT JSON_VALUE(i.meta, '$.list_unsubscribe' RETURNING VARCHAR2(4000)),
                           JSON_VALUE(i.meta, '$.list_unsubscribe_post' RETURNING VARCHAR2(4000)),
                           JSON_VALUE(i.meta, '$.list_id' RETURNING VARCHAR2(4000))
                      FROM items i
                     WHERE LOWER(i.sender_addr) = :addr AND i.is_from_me = FALSE
                       AND JSON_EXISTS(i.meta, '$.list_unsubscribe') AND {EXCLUDE_UNSAFE}
                     ORDER BY i.received_at DESC FETCH FIRST 1 ROWS ONLY""", {"addr": addr})
    r = cur.fetchone()
    if not r:
        return None
    return {**parse_list_unsubscribe(r[0], r[1]), "list_id": r[2]}


def sender_for_id(conn: oracledb.Connection, uid: int) -> str | None:
    cur = conn.cursor()
    cur.execute("SELECT sender_addr FROM unsubscribes WHERE id = :id", {"id": int(uid)})
    r = cur.fetchone()
    return r[0] if r else None


def preview(conn: oracledb.Connection, sender_addr: str) -> dict:
    """What `act(..., 'unsubscribe')` would do, without doing it."""
    addr = (sender_addr or "").strip().lower()
    cur = conn.cursor()
    if _sender_flagged(cur, addr):
        return {"sender_addr": addr, "ok": False, "would": "refuse",
                "detail": "this sender has been flagged spam/suspicious; unsubscribing would confirm your address"}
    h = _latest_header(cur, addr)
    if not h or not h["method"]:
        return {"sender_addr": addr, "ok": False, "would": "refuse", "detail": "no usable List-Unsubscribe header"}
    would = {"one_click": "POST List-Unsubscribe=One-Click to the sender's https endpoint",
             "url": "hand you the unsubscribe page to open yourself",
             "mailto": "hand you the unsubscribe address (emAIl never sends email)"}[h["method"]]
    return {"sender_addr": addr, "ok": True, "method": h["method"], "target": h["target"], "would": would}


def act(conn: oracledb.Connection, sender_addr: str, action: str, actor: str = "user") -> dict:
    """User decision on a suggestion. 'unsubscribe': one-click POST if offered, otherwise mark 'manual' and return
    the link for the user. 'dismiss': keep getting these, never suggest again."""
    if action not in ("unsubscribe", "dismiss"):
        raise ValueError("action must be unsubscribe or dismiss")
    addr = (sender_addr or "").strip().lower()
    if not addr:
        raise ValueError("sender_addr is required")
    cur = conn.cursor()
    cur.execute("SELECT id FROM unsubscribes WHERE sender_addr = :addr", {"addr": addr})
    r = cur.fetchone()
    uid = int(r[0]) if r else None
    if action == "dismiss":
        res = {"sender_addr": addr, "status": "dismissed", "detail": "kept: won't be suggested again", "link": None}
        method = target = list_id = None
    else:
        p = preview(conn, addr)
        if not p["ok"]:
            res = {"sender_addr": addr, "status": "failed", "detail": p["detail"], "link": None}
            method = target = list_id = None
        else:
            method, target = p["method"], p["target"]
            list_id = (_latest_header(cur, addr) or {}).get("list_id")
            if method == "one_click":
                ok, detail = one_click_unsubscribe(target)
                res = {"sender_addr": addr, "status": "done" if ok else "failed", "detail": detail,
                       "link": None if ok else _manual_fallback(target)}
            else:
                res = {"sender_addr": addr, "status": "manual", "link": target,
                       "detail": "open the link to unsubscribe" if method == "url"
                       else "send an email to this address to unsubscribe"}
    binds = {"addr": addr, "status": res["status"], "detail": res["detail"][:1000], "method": method,
             "target": target, "list_id": (list_id or "")[:1000] or None}
    if uid:
        cur.execute("""UPDATE unsubscribes SET status = :status, detail = :detail, acted_at = SYSTIMESTAMP,
                              method = NVL(:method, method), target = NVL(:target, target),
                              list_id = NVL(:list_id, list_id)
                        WHERE sender_addr = :addr""", binds)
    else:
        cur.execute("""INSERT INTO unsubscribes (sender_addr, list_id, method, target, status, detail, acted_at)
                       VALUES (:addr, :list_id, :method, :target, :status, :detail, SYSTIMESTAMP)""", binds)
    store.audit(conn, actor, f"unsubscribe_{action}", addr, {"status": res["status"], "method": method})
    _COUNTS.clear()
    return res


def _manual_fallback(target: str) -> str | None:
    """After a failed one-click, the same https URL is still a page the user can open themselves."""
    return target if target and target.lower().startswith("https://") else None


def act_id(conn: oracledb.Connection, uid: int, action: str, actor: str = "user") -> dict | None:
    addr = sender_for_id(conn, uid)
    return act(conn, addr, action, actor) if addr else None


# ---------- follow-up nudges ----------

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_ASKS = re.compile(r"\b(let me know|can you|could you|would you|will you|are you able|please (confirm|advise|"
                   r"let me|send|reply|respond)|any (update|news|word)s?|get back to me|what do you think|"
                   r"your thoughts|thoughts\?|when (can|could|will) you|do you (know|have))\b", re.I)


def expects_reply(text: str) -> bool:
    """Does an email you wrote ask something of the recipient? Deliberately simple: a question mark in your own
    words (URLs removed - query strings have '?'), or a request phrase ("let me know", "could you", "any update")."""
    t = _URL.sub(" ", text or "")[:2000]
    return "?" in t or bool(_ASKS.search(t))


def _first_to(recipients) -> dict | None:
    to = (recipients or {}).get("to") or [] if isinstance(recipients, dict) else []
    return to[0] if to else None


def followup_nudges(conn: oracledb.Connection, days_min: int = 3, days_max: int = 21, limit: int = 10) -> list[dict]:
    """Emails you sent 3-21 days ago that ask something of a real person, where nobody else has written in the
    thread since (and you haven't followed up yourself). Not noreply/list addresses, not yourself, not dismissed."""
    cur = conn.cursor()
    cur.execute("""SELECT i.id, i.thread_id, i.recipients, i.subject, i.received_at,
                          NVL(DBMS_LOB.SUBSTR(i.body_text, 1000, 1), i.snippet)
                     FROM items i
                    WHERE i.is_from_me = TRUE AND i.thread_id IS NOT NULL AND i.nudge_dismissed_at IS NULL
                      AND i.received_at < SYSTIMESTAMP - NUMTODSINTERVAL(:dmin, 'DAY')
                      AND i.received_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:dmax, 'DAY')
                      AND NVL(JSON_SERIALIZE(i.labels), '[]') NOT LIKE '%"DRAFT"%'
                      AND NVL(JSON_SERIALIZE(i.labels), '[]') NOT LIKE '%"TRASH"%'
                      AND NOT EXISTS (SELECT 1 FROM items r WHERE r.thread_id = i.thread_id
                                         AND r.received_at > i.received_at AND r.id <> i.id)
                    ORDER BY i.received_at DESC FETCH FIRST 200 ROWS ONLY""",
                {"dmin": int(days_min), "dmax": int(days_max)})
    rows = cur.fetchall()
    cur.execute("SELECT LOWER(address) FROM accounts")
    mine = {r[0] for r in cur}
    cands = []
    for item_id, thread_id, recips, subject, sent, body in rows:
        to = _first_to(recips)
        addr = (to or {}).get("addr", "").lower()
        if not addr or addr in mine or triage._NOREPLY.search(addr):
            continue
        if not expects_reply(f"{subject or ''}\n{body or ''}"):
            continue
        cands.append((item_id, thread_id, to, addr, subject, sent))
    if not cands:
        return []
    binds = {f"a{n}": c[3] for n, c in enumerate({c[3]: c for c in cands}.values())}
    cur.execute(f"""SELECT DISTINCT LOWER(sender_addr) FROM items
                     WHERE LOWER(sender_addr) IN ({",".join(":" + b for b in binds)})
                       AND JSON_EXISTS(meta, '$.list_unsubscribe')""", binds)
    lists = {r[0] for r in cur}                        # replying to a newsletter isn't waiting on a person
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for item_id, thread_id, to, addr, subject, sent in cands:
        if addr in lists:
            continue
        sent_naive = sent.replace(tzinfo=None) if sent else None
        out.append({"item_id": item_id, "thread_id": thread_id, "to": to.get("name") or addr, "to_addr": addr,
                    "subject": subject or "(no subject)", "sent_at": str(sent)[:16] if sent else "",
                    "days_waiting": (now - sent_naive).days if sent_naive else 0})
        if len(out) >= limit:
            break
    return out


def dismiss_nudge(conn: oracledb.Connection, item_id: int) -> bool:
    cur = conn.cursor()
    cur.execute("UPDATE items SET nudge_dismissed_at = SYSTIMESTAMP WHERE id = :id AND is_from_me = TRUE",
                {"id": int(item_id)})
    _COUNTS.clear()
    return cur.rowcount > 0


# ---------- dashboard counts (cached: the status panel refreshes every 30 s) ----------

_COUNTS: dict[int, tuple[float, dict]] = {}
COUNTS_TTL = 600


def counts(conn: oracledb.Connection, user_id: int) -> dict:
    """{"unsubs": n, "followups": n} for the home panel; recomputed at most every 10 minutes per user."""
    hit = _COUNTS.get(user_id)
    if hit and time.monotonic() - hit[0] < COUNTS_TTL:
        return hit[1]
    c = {"unsubs": len(unsubscribe_candidates(conn)), "followups": len(followup_nudges(conn, limit=50))}
    _COUNTS[user_id] = (time.monotonic(), c)
    return c
