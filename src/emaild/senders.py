"""Cold start: who matters to you, learned from your own sent mail. Recomputed per user from stored items."""
from __future__ import annotations

import oracledb

REFRESH_SQL = """
INSERT INTO sender_stats (sender_addr, received, replied, sent_to, avg_reply_hours, last_received, last_contact)
WITH recv AS (
    SELECT LOWER(sender_addr) addr, COUNT(*) received, MAX(received_at) last_received
      FROM items WHERE is_from_me = FALSE AND sender_addr IS NOT NULL GROUP BY LOWER(sender_addr)
), repl AS (
    SELECT LOWER(p.sender_addr) addr, COUNT(*) replied,
           AVG((CAST(m.received_at AS DATE) - CAST(p.received_at AS DATE)) * 24) avg_hours
      FROM items m JOIN items p ON p.rfc_message_id = m.in_reply_to AND p.account_id = m.account_id
     WHERE m.is_from_me = TRUE AND p.is_from_me = FALSE AND m.in_reply_to IS NOT NULL
     GROUP BY LOWER(p.sender_addr)
), sent AS (
    SELECT LOWER(r.addr) addr, COUNT(*) sent_to, MAX(m.received_at) last_contact
      FROM items m,
           JSON_TABLE(m.recipients, '$.to[*]' COLUMNS (addr VARCHAR2(320) PATH '$.addr')) r
     WHERE m.is_from_me = TRUE AND r.addr IS NOT NULL
     GROUP BY LOWER(r.addr)
), addrs AS (
    SELECT addr FROM recv UNION SELECT addr FROM sent
)
SELECT a.addr, NVL(recv.received, 0), NVL(repl.replied, 0), NVL(sent.sent_to, 0), repl.avg_hours,
       recv.last_received, sent.last_contact
  FROM addrs a
  LEFT JOIN recv ON recv.addr = a.addr
  LEFT JOIN repl ON repl.addr = a.addr
  LEFT JOIN sent ON sent.addr = a.addr
"""


def refresh(conn: oracledb.Connection) -> int:
    """Rebuild this user's sender stats (VPD scopes both the delete and the insert to the current user)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM sender_stats")
    cur.execute(REFRESH_SQL)
    n = cur.rowcount
    conn.commit()
    return n


def get(conn: oracledb.Connection, addr: str | None) -> dict:
    if not addr:
        return {}
    cur = conn.cursor()
    cur.execute("""SELECT received, replied, sent_to, avg_reply_hours FROM sender_stats
                   WHERE sender_addr = :1""", [addr.lower()])
    r = cur.fetchone()
    if not r:
        return {"received": 0, "replied": 0, "sent_to": 0, "avg_reply_hours": None}
    return {"received": r[0], "replied": r[1], "sent_to": r[2],
            "avg_reply_hours": round(float(r[3]), 1) if r[3] is not None else None}


def top(conn: oracledb.Connection, limit: int = 20) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""SELECT sender_addr, received, replied, sent_to, avg_reply_hours FROM sender_stats
                   ORDER BY replied * 3 + sent_to DESC, received DESC FETCH FIRST :1 ROWS ONLY""", [limit])
    return [{"sender": r[0], "received": r[1], "replied": r[2], "sent_to": r[3],
             "avg_reply_hours": round(float(r[4]), 1) if r[4] is not None else None} for r in cur]
