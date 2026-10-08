"""Numbered lists: every reply that lists emails numbers them [1], [2], ... and remembers which email each number
is, so "/show 3" (or replying "3" to that list) opens the right one.

Context lives in `list_context` (migration 016), one row per list message: Telegram stores each sent list under
"<chat_id>:<message_id>" (so replying to an OLDER list still resolves against that list), the CLI under 'last'.
The newest row per channel is the "latest list". Rows are user-scoped by VPD like everything else, and only the
newest KEEP per user per channel are kept (pruned on insert).

Before migration 016 numbering still renders; remember() quietly does nothing and resolve() raises Unavailable so
callers can say "run emaild migrate".
"""
from __future__ import annotations

import json
import logging
import re

import oracledb

log = logging.getLogger(__name__)

CHANNELS = ("telegram", "cli", "web", "mcp")
KEEP = 50                    # list contexts kept per user per channel
BARE_NUMBER_MINUTES = 30     # a bare "3" only means "/show 3" while the latest list is this fresh
MAX_ITEMS = 100


class Unavailable(RuntimeError):
    """The list_context table doesn't exist yet (before migration 016)."""


def _missing_table(e: Exception) -> bool:
    return "ORA-00942" in str(e) or "ORA-00904" in str(e)


def tag(n: int) -> str:
    return f"[{int(n)}]"


def number(items: list[str], start: int = 1) -> list[str]:
    """['a', 'b'] -> ['[1] a', '[2] b'] (plain text; callers escape before or after as their surface needs)."""
    return [f"{tag(n)} {s}" for n, s in enumerate(items, start)]


def renumber_citations(lines: list[str], start: int = 1) -> tuple[list[str], list[int]]:
    """Project/thread status cites emails as ' [email 123]'. For chat surfaces those become running numbers
    ' [1]' (the same email cited twice keeps its number) and the item ids come back in order, to remember()."""
    ids: list[int] = []

    def sub(m: re.Match) -> str:
        iid = int(m.group(1))
        if iid not in ids:
            ids.append(iid)
        return tag(start + ids.index(iid))

    out = [re.sub(r"\[email (\d+)\]", sub, line) for line in lines]
    return out, ids


def _ids(v) -> list[int]:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return [int(x) for x in (v or []) if isinstance(x, (int, float, str)) and str(x).strip().lstrip("-").isdigit()]


def remember(conn, channel: str, ref: str, item_ids: list) -> bool:
    """Store one list's item ids (in display order) and prune this channel to the newest KEEP. Best effort:
    False (and nothing raised) before migration 016 or with nothing to remember."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    ids = [int(i) for i in item_ids if i is not None][:MAX_ITEMS]
    if not ids:
        return False
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO list_context (channel, ref, item_ids) VALUES (:ch, :ref, :ids)",
                    {"ch": channel, "ref": str(ref)[:100], "ids": json.dumps(ids)})
        cur.execute("""DELETE FROM list_context WHERE channel = :ch AND id NOT IN (
                           SELECT id FROM list_context WHERE channel = :ch
                            ORDER BY created_at DESC, id DESC FETCH FIRST :keep ROWS ONLY)""",
                    {"ch": channel, "keep": KEEP})
    except oracledb.DatabaseError as e:
        if _missing_table(e):
            log.debug("list_context unavailable (run 'emaild migrate'): %s", e)
            return False
        raise
    return True


def get(conn, channel: str, ref: str | None = None) -> dict | None:
    """{"item_ids", "age_minutes"} for the list `ref` (or the newest one in the channel); None when there's none.
    Raises Unavailable before migration 016."""
    binds: dict = {"ch": channel}
    where = "channel = :ch"
    if ref is not None:
        where += " AND ref = :ref"
        binds["ref"] = str(ref)[:100]
    cur = conn.cursor()
    try:
        cur.execute(f"""SELECT item_ids,
                               ROUND((CAST(SYSTIMESTAMP AS DATE) - CAST(created_at AS DATE)) * 1440)
                          FROM list_context WHERE {where}
                         ORDER BY created_at DESC, id DESC FETCH FIRST 1 ROWS ONLY""", binds)
        r = cur.fetchone()
    except oracledb.DatabaseError as e:
        if _missing_table(e):
            raise Unavailable("Opening emails by number needs a database update: run 'emaild migrate'.") from e
        raise
    if not r:
        return None
    return {"item_ids": _ids(r[0]), "age_minutes": float(r[1] or 0)}


def resolve(conn, channel: str, n: int, ref: str | None = None, max_age_minutes: float | None = None) -> int | None:
    """Item id of number `n` in list `ref` (or the latest list of the channel). None when there's no such list,
    the number is out of range, or the latest list is older than `max_age_minutes`. Raises Unavailable before
    migration 016."""
    ctx = get(conn, channel, ref)
    if ctx is None:
        return None
    if max_age_minutes is not None and ctx["age_minutes"] > max_age_minutes:
        return None
    ids = ctx["item_ids"]
    return ids[n - 1] if 1 <= int(n) <= len(ids) else None


# "show 3", "3", "full 3", "show 3 full", "thread 3", "/show 3" (the slash form is parsed by the command handler)
_REQ = re.compile(r"^(?:(?P<verb>show|open|full|thread|#)\s*)?#?(?P<n>\d{1,3})(?:\s+(?P<full>full))?$", re.I)


def parse_request(text: str) -> dict | None:
    """'show 3' -> {"n": 3, "mode": "card", "bare": False}; '3' -> bare; 'full 3' / 'show 3 full' -> full;
    'thread 3' -> thread. None for anything else (then the text is a normal question)."""
    t = re.sub(r"\s+", " ", text or "").strip().rstrip(".!?")
    m = _REQ.match(t)
    if not m:
        return None
    verb = (m.group("verb") or "").lower()
    mode = "thread" if verb == "thread" else "full" if (verb == "full" or m.group("full")) else "card"
    return {"n": int(m.group("n")), "mode": mode, "bare": not verb}
