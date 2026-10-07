"""Reading threads and raw messages (the escape hatch)."""
from __future__ import annotations

import oracledb

from . import blobstore, store
from .db import UserCtx


def get_thread(conn: oracledb.Connection, item_id: int, max_chars_per_message: int = 4000) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT thread_id FROM items WHERE id = :1", [item_id])
    r = cur.fetchone()
    if not r:
        return None
    cur.execute("""SELECT id, received_at, sender_name, sender_addr, subject, NVL(body_text, full_text), attachments, is_from_me
                     FROM items WHERE thread_id = :1 ORDER BY received_at""", [r[0]])
    messages = []
    for m in cur:
        messages.append({"item_id": m[0], "date": str(m[1]) if m[1] else "",
                         "from": f"{m[2]} <{m[3]}>" if m[2] else m[3], "subject": m[4],
                         "from_me": bool(m[7]), "text": (m[5] or "")[:max_chars_per_message],
                         "attachments": [a.get("filename") for a in (m[6] or [])]})
    return {"thread_id": r[0], "messages": messages}


def show_raw(conn: oracledb.Connection, ctx: UserCtx, item_id: int, include_mime: bool = False,
             actor: str = "mcp") -> dict | None:
    cur = conn.cursor()
    cur.execute("""SELECT id, received_at, sender_name, sender_addr, recipients, subject, full_text, attachments,
                          labels, meta, blob_path FROM items WHERE id = :1""", [item_id])
    r = cur.fetchone()
    if not r:
        return None
    store.audit(conn, actor, "show_raw", str(item_id), {"include_mime": include_mime})  # implicit "I looked" signal
    out = {"item_id": r[0], "date": str(r[1]) if r[1] else "", "from": f"{r[2]} <{r[3]}>" if r[2] else r[3],
           "recipients": r[4], "subject": r[5], "text": r[6] or "", "attachments": r[7], "labels": r[8],
           "headers_of_note": r[9]}
    if include_mime and r[10]:
        out["mime"] = blobstore.get(ctx, r[10]).decode("utf-8", errors="replace")
    return out
