"""Writes and reads against the content tables. Callers pass a user-scoped connection (VPD applies)."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import oracledb

from . import blobstore
from .db import UserCtx
from .models import Item
from .normalise import chunk_text


def _utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def audit(conn: oracledb.Connection, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    conn.cursor().execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (:1, :2, :3, :4)",
                          [actor, action, target[:200] or None, json.dumps(detail or {})])


# ---------- accounts ----------

def upsert_account(conn: oracledb.Connection, provider: str, address: str, token_enc: bytes) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM accounts WHERE provider = :1 AND address = :2", [provider, address])
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE accounts SET token_enc = :1, status = 'active', last_error = NULL WHERE id = :2",
                    [token_enc, row[0]])
        return row[0]
    out = cur.var(oracledb.NUMBER)
    cur.execute("INSERT INTO accounts (provider, address, token_enc) VALUES (:1, :2, :3) RETURNING id INTO :4",
                [provider, address, token_enc, out])
    return int(out.getvalue()[0])


def get_account(conn: oracledb.Connection, account_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute("""SELECT id, provider, address, status, token_enc, sync_cursor, backfill_token, backfill_done,
                          sync_state
                     FROM accounts WHERE id = :1""", [account_id])
    r = cur.fetchone()
    if not r:
        return None
    token = r[4].read() if hasattr(r[4], "read") else r[4]
    state = r[8] if isinstance(r[8], dict) or r[8] is None else json.loads(r[8])
    return dict(id=r[0], provider=r[1], address=r[2], status=r[3], token_enc=token,
                sync_cursor=r[5], backfill_token=r[6], backfill_done=bool(r[7]), sync_state=state)


def update_account(conn: oracledb.Connection, account_id: int, **fields) -> None:
    allowed = {"token_enc", "sync_cursor", "backfill_token", "backfill_done", "last_sync_at", "last_error", "status",
               "sync_state"}
    sets, binds = [], {}
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(k)
        sets.append(f"{k} = :{k}")
        binds[k] = v
    if not sets:
        return
    binds["id"] = account_id
    cur = conn.cursor()
    if "sync_state" in binds:  # bound as native JSON: delta links can push it past a VARCHAR2 bind's 4000 bytes
        cur.setinputsizes(sync_state=oracledb.DB_TYPE_JSON)
    cur.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = :id", binds)


# ---------- items ----------

def item_exists(conn: oracledb.Connection, account_id: int, provider_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM items WHERE account_id = :1 AND provider_id = :2", [account_id, provider_id])
    return cur.fetchone() is not None


def existing_ids(conn: oracledb.Connection, account_id: int, provider_ids: list[str]) -> set[str]:
    if not provider_ids:
        return set()
    cur = conn.cursor()
    found: set[str] = set()
    for i in range(0, len(provider_ids), 500):
        batch = provider_ids[i:i + 500]
        binds = ",".join(f":p{j}" for j in range(len(batch)))
        params = {f"p{j}": v for j, v in enumerate(batch)}
        params["acc"] = account_id
        cur.execute(f"SELECT provider_id FROM items WHERE account_id = :acc AND provider_id IN ({binds})", params)
        found.update(r[0] for r in cur)
    return found


def _thread_id(cur: oracledb.Cursor, account_id: int, item: Item) -> int:
    at = _utc(item.received_at or item.sent_at)
    cur.execute("SELECT id FROM threads WHERE account_id = :1 AND provider_thread_id = :2",
                [account_id, item.provider_thread_id])
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE threads SET message_count = message_count + 1,
                              last_at = GREATEST(NVL(last_at, :at), :at),
                              first_at = LEAST(NVL(first_at, :at), :at)
                        WHERE id = :id""", {"at": at, "id": row[0]})
        return row[0]
    out = cur.var(oracledb.NUMBER)
    cur.execute("""INSERT INTO threads (account_id, provider_thread_id, subject, first_at, last_at, message_count)
                   VALUES (:acc, :ptid, :subj, :at, :at, 1) RETURNING id INTO :out""",
                {"acc": account_id, "ptid": item.provider_thread_id, "subj": item.subject or None, "at": at, "out": out})
    return int(out.getvalue()[0])


def insert_item(conn: oracledb.Connection, ctx: UserCtx, account_id: int, account_address: str, item: Item) -> int:
    cur = conn.cursor()
    thread_id = _thread_id(cur, account_id, item)
    blob_path = blobstore.put(ctx, account_id, item.provider_id, item.raw) if item.raw else None
    sender = item.sender
    is_from_me = "SENT" in item.labels or (sender is not None and sender.addr == account_address)
    out = cur.var(oracledb.NUMBER)
    cur.execute("""
        INSERT INTO items (account_id, thread_id, channel, provider_id, rfc_message_id, in_reply_to,
                           sender_addr, sender_name, recipients, subject, sent_at, received_at, snippet,
                           body_text, full_text, labels, meta, attachments, is_from_me, size_bytes, blob_path)
        VALUES (:account_id, :thread_id, :channel, :provider_id, :rfc_message_id, :in_reply_to,
                :sender_addr, :sender_name, :recipients, :subject, :sent_at, :received_at, :snippet,
                :body_text, :full_text, :labels, :meta, :attachments, :is_from_me, :size_bytes, :blob_path)
        RETURNING id INTO :out""", {
        "account_id": account_id, "thread_id": thread_id, "channel": item.channel,
        "provider_id": item.provider_id, "rfc_message_id": item.rfc_message_id or None,
        "in_reply_to": item.in_reply_to or None,
        "sender_addr": sender.addr[:320] if sender else None, "sender_name": sender.name[:500] if sender else None,
        "recipients": json.dumps({"to": [asdict(a) for a in item.to], "cc": [asdict(a) for a in item.cc]}),
        "subject": item.subject or None, "sent_at": _utc(item.sent_at), "received_at": _utc(item.received_at),
        "snippet": item.snippet or None, "body_text": item.body_text or None, "full_text": item.full_text or None,
        "labels": json.dumps(item.labels), "meta": json.dumps(item.meta),
        "attachments": json.dumps([asdict(a) for a in item.attachments]),
        "is_from_me": bool(is_from_me), "size_bytes": item.size_bytes, "blob_path": blob_path, "out": out})
    item_id = int(out.getvalue()[0])
    chunks = chunk_text(item.subject, item.body_text or item.full_text or item.snippet)
    if chunks:
        cur.executemany("INSERT INTO chunks (item_id, seq, content) VALUES (:1, :2, :3)",
                        [[item_id, i, c] for i, c in enumerate(chunks)])
    return item_id


def update_labels(conn: oracledb.Connection, account_id: int, provider_id: str, labels: list[str]) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE items SET labels = :1 WHERE account_id = :2 AND provider_id = :3",
                [json.dumps(labels), account_id, provider_id])
    if "SPAM" in labels:  # marked as spam by the provider or the user (Gmail Spam / Outlook Junk): reclassify
        cur.execute("""UPDATE decisions SET category = 'spam', action = 'archive', importance = 'low',
                              needs_review = FALSE, reasons = 'Your mail provider marked this as spam.'
                        WHERE status = 'proposed' AND item_id IN
                              (SELECT id FROM items WHERE account_id = :1 AND provider_id = :2)""",
                    [account_id, provider_id])


def mark_deleted(conn: oracledb.Connection, account_id: int, provider_id: str) -> None:
    cur = conn.cursor()
    cur.execute("SELECT labels FROM items WHERE account_id = :1 AND provider_id = :2", [account_id, provider_id])
    row = cur.fetchone()
    if row is None:
        return
    labels = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
    if "_DELETED" not in labels:
        update_labels(conn, account_id, provider_id, labels + ["_DELETED"])


# ---------- sync failures ----------

MAX_ATTEMPTS = 3


def record_failure(conn: oracledb.Connection, account_id: int, provider_id: str, error: str) -> None:
    conn.cursor().execute("""
        MERGE INTO sync_failures f USING (SELECT :acc AS account_id, :pid AS provider_id FROM dual) s
           ON (f.account_id = s.account_id AND f.provider_id = s.provider_id)
         WHEN MATCHED THEN UPDATE SET attempts = attempts + 1, last_error = :err, last_at = SYSTIMESTAMP
         WHEN NOT MATCHED THEN INSERT (account_id, provider_id, last_error) VALUES (:acc, :pid, :err)""",
        {"acc": account_id, "pid": provider_id, "err": error[:2000]})


def clear_failure(conn: oracledb.Connection, account_id: int, provider_id: str) -> None:
    conn.cursor().execute("DELETE FROM sync_failures WHERE account_id = :1 AND provider_id = :2",
                          [account_id, provider_id])


def retryable_failures(conn: oracledb.Connection, account_id: int, limit: int = 50) -> list[str]:
    cur = conn.cursor()
    cur.execute("""SELECT provider_id FROM sync_failures WHERE account_id = :acc AND attempts < :max_attempts
                   ORDER BY last_at FETCH FIRST :lim ROWS ONLY""", {"acc": account_id, "max_attempts": MAX_ATTEMPTS, "lim": limit})
    return [r[0] for r in cur]


def failure_summary(conn: oracledb.Connection, account_id: int) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT COUNT(*), COUNT(CASE WHEN attempts >= :max_attempts THEN 1 END) FROM sync_failures
                   WHERE account_id = :acc""", {"acc": account_id, "max_attempts": MAX_ATTEMPTS})
    total, gave_up = cur.fetchone()
    cur.execute("""SELECT SUBSTR(last_error, 1, 120) e, COUNT(*) n FROM sync_failures WHERE account_id = :1
                   GROUP BY SUBSTR(last_error, 1, 120) ORDER BY n DESC FETCH FIRST 5 ROWS ONLY""", [account_id])
    return {"failed": total, "gave_up": gave_up, "top_errors": [{"error": r[0], "count": r[1]} for r in cur]}


# ---------- embeddings ----------

def active_model(conn: oracledb.Connection) -> tuple[int, str] | None:
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM embedding_models WHERE active = TRUE")
    r = cur.fetchone()
    return (r[0], r[1]) if r else None


def embed_pending(conn: oracledb.Connection, batch: int) -> int:
    """Fill embeddings for up to `batch` chunks of the current user using the in-DB ONNX model."""
    model = active_model(conn)
    if model is None:
        return 0
    model_id, name = model
    cur = conn.cursor()
    cur.execute(f"""UPDATE chunks SET embedding = VECTOR_EMBEDDING({name} USING content AS data), model_id = :mid
                    WHERE id IN (SELECT id FROM chunks WHERE model_id IS NULL OR model_id <> :mid
                                 FETCH FIRST :n ROWS ONLY)""", {"mid": model_id, "n": batch})
    return cur.rowcount


# ---------- status ----------

def status(conn: oracledb.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT a.id, a.provider, a.address, a.status, a.backfill_done, a.last_sync_at, a.last_error,
                          (SELECT COUNT(*) FROM items i WHERE i.account_id = a.id)
                     FROM accounts a ORDER BY a.id""")
    accounts = [dict(id=r[0], provider=r[1], address=r[2], status=r[3], backfill_done=bool(r[4]),
                     last_sync_at=str(r[5]) if r[5] else None, last_error=r[6], items=r[7])
                for r in cur]
    model = active_model(conn)
    # VECTOR columns can't be aggregated (ORA-22849); model_id is set together with the embedding.
    cur.execute("SELECT COUNT(*), COUNT(CASE WHEN model_id = :mid THEN 1 END) FROM chunks",
                {"mid": model[0] if model else -1})
    total, embedded = cur.fetchone()
    return {"accounts": accounts, "chunks": total, "chunks_embedded": embedded,
            "embedding_backlog": total - embedded, "embedding_model": model[1] if model else None}
