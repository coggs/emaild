"""Sync loop: backfill then incremental polling, per account, inside the owning user's VPD context."""
from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone

from . import crypto, db, store
from .channels.base import Budget, BudgetExhausted, Channel, CursorExpired, RateLimited, ReauthRequired
from googleapiclient.errors import HttpError

from .channels.gmail import GmailChannel
from .channels.outlook import OutlookChannel
from .config import settings
from .db import UserCtx

log = logging.getLogger(__name__)

# Per account per cycle, keeps the loop responsive during backfill. Gmail counts message fetch attempts;
# Outlook counts every HTTP attempt (list/delta pages, metadata, MIME, retries, token refreshes) via a Budget.
MAX_PER_RUN = 400


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _encrypt_creds(ctx: UserCtx, creds: dict) -> bytes:
    s = settings()
    return crypto.encrypt(s.master_key, ctx.tenant_id, ctx.user_id, json.dumps(creds).encode())


def open_channel(ctx: UserCtx, account: dict, conn=None, budget: Budget | None = None) -> Channel:
    """The connector for this account. With `conn`, rotated Outlook refresh tokens are saved the moment they arrive."""
    s = settings()
    info = json.loads(crypto.decrypt(s.master_key, ctx.tenant_id, ctx.user_id, account["token_enc"]))
    if account["provider"] == "gmail":
        return GmailChannel(info)
    if account["provider"] == "outlook":
        def save(creds: dict) -> None:
            store.update_account(conn, account["id"], token_enc=_encrypt_creds(ctx, creds))
            conn.commit()
        return OutlookChannel(info, account.get("sync_state"), budget=budget,
                              on_credentials=save if conn is not None else None)
    raise NotImplementedError(account["provider"])


def _persist_creds(conn, ctx: UserCtx, account_id: int, ch: Channel) -> None:
    creds = ch.updated_credentials()
    if creds:
        store.update_account(conn, account_id, token_enc=_encrypt_creds(ctx, creds))


def _store_ids(conn, ctx: UserCtx, account: dict, ch: Channel, ids: list[str], budget: int) -> tuple[int, int, int]:
    """Fetch and store ids not already stored, up to `budget` fetch attempts.

    Returns (stored, attempted, left_unprocessed). Every attempt counts against the budget, successful or not,
    so failures can't turn one cycle into a burst through the whole mailbox (Gmail per-user quota).
    A message that fails is recorded in sync_failures and retried on later cycles (up to 3 attempts).
    Outlook also charges each request to its Budget and stops the cycle with BudgetExhausted / RateLimited.
    """
    existing = store.existing_ids(conn, account["id"], ids)
    new = [i for i in ids if i not in existing]
    batch = new[:budget]
    stored = 0
    for pid in batch:
        try:
            item = ch.fetch(pid)
            store.insert_item(conn, ctx, account["id"], account["address"], item)
        except (BudgetExhausted, RateLimited, ReauthRequired):
            conn.rollback()
            raise  # not this message's fault: stop the cycle; it is fetched next cycle
        except HttpError as e:
            if e.resp.status in (403, 429) and "rate" in str(e).lower():
                raise  # quota: stop this account's cycle; the page is retried next cycle
            _fail(conn, account, pid, e)
            continue
        except Exception as e:
            _fail(conn, account, pid, e)
            continue
        store.clear_failure(conn, account["id"], pid)
        conn.commit()
        stored += 1
    return stored, len(batch), len(new) - len(batch)


def _fail(conn, account: dict, pid: str, e: Exception) -> None:
    conn.rollback()
    log.warning("account %s message %s failed: %s: %s", account["id"], pid, type(e).__name__, str(e)[:300])
    store.record_failure(conn, account["id"], pid, f"{type(e).__name__}: {e}")
    conn.commit()


def _retry_failures(conn, ctx: UserCtx, account: dict, ch: Channel) -> int:
    ids = store.retryable_failures(conn, account["id"])
    if not ids:
        return 0
    n, _, _ = _store_ids(conn, ctx, account, ch, ids, len(ids))
    return n


def _pull_gmail(conn, ctx: UserCtx, account: dict, ch: GmailChannel, stats: dict) -> None:
    s = settings()
    account_id = account["id"]
    budget = MAX_PER_RUN
    if not account["backfill_done"]:
        stats["phase"] = "backfill"
        if not account["sync_cursor"]:
            # record the incremental starting point BEFORE backfilling so nothing slips between
            _, cursor = ch.identity()
            store.update_account(conn, account_id, sync_cursor=cursor)
            conn.commit()  # keep the cursor even if a later message fails and rolls back
        token = account["backfill_token"]
        while budget > 0:
            page = ch.list_page(s.backfill_days, token)
            n, attempted, left = _store_ids(conn, ctx, account, ch, page.ids, budget)
            stats["stored"] += n
            budget -= attempted
            if left:
                break  # page not finished; resume this page next cycle
            token = page.next_token
            store.update_account(conn, account_id, backfill_token=token)
            conn.commit()
            if not token:
                store.update_account(conn, account_id, backfill_done=True, backfill_token=None)
                break
    else:
        stats["phase"] = "incremental"
        cursor, page_token = account["sync_cursor"], None
        new_cursor = cursor
        while True:
            ch_ = ch.changes(cursor, page_token)
            n, _, _ = _store_ids(conn, ctx, account, ch, list(dict.fromkeys(ch_.added)), 10_000)
            stats["stored"] += n
            for pid, labels in ch_.labels.items():
                store.update_labels(conn, account_id, pid, labels)
                stats["labels"] += 1
            for pid in ch_.deleted:
                store.mark_deleted(conn, account_id, pid)
                stats["deleted"] += 1
            conn.commit()
            new_cursor = ch_.cursor or new_cursor
            page_token = ch_.next_token
            if not page_token:
                break
        store.update_account(conn, account_id, sync_cursor=new_cursor)


def _pull_outlook(conn, ctx: UserCtx, account: dict, ch: OutlookChannel, stats: dict) -> None:
    """Per-folder Graph delta rounds (the first, window-filtered round is the backfill). See channels/outlook.py.

    A folder's saved link advances only after its page is fully processed, so stopping anywhere (budget, throttling,
    crash) resumes from the same page; already-stored messages are skipped by id.
    """
    s = settings()
    account_id = account["id"]

    def save_state() -> None:
        store.update_account(conn, account_id, sync_state=ch.state)
        conn.commit()

    stats["phase"] = "incremental" if account["backfill_done"] else "backfill"
    try:
        folders = ch.folders()
        for folder in folders:
            resynced = False
            while True:
                try:
                    page = ch.delta(folder, s.backfill_days)
                except CursorExpired as e:
                    if resynced:
                        raise
                    log.warning("account %s: Outlook delta for %s expired (%s); re-backfilling that folder",
                                account_id, folder, e)
                    ch.reset_folder(folder)
                    save_state()
                    resynced = True
                    stats["resynced"] = stats.get("resynced", 0) + 1
                    continue
                ids = list(dict.fromkeys(page.added))
                existing = store.existing_ids(conn, account_id, ids)
                for pid in ids:  # changes to stored messages: flags, read state, moves (e.g. into Junk -> SPAM)
                    if pid in existing and pid in page.labels:
                        store.update_labels(conn, account_id, pid, page.labels[pid])
                        stats["labels"] += 1
                conn.commit()
                n, _, _ = _store_ids(conn, ctx, account, ch, ids, len(ids))
                stats["stored"] += n
                for pid in dict.fromkeys(page.deleted):  # left this folder: deleted, or moved somewhere else
                    if not store.item_exists(conn, account_id, pid):
                        continue
                    labels = ch.locate(pid)
                    if labels is None:
                        store.mark_deleted(conn, account_id, pid)
                        stats["deleted"] += 1
                    else:
                        store.update_labels(conn, account_id, pid, labels)
                        stats["labels"] += 1
                    conn.commit()
                ch.advance(folder, page)
                save_state()
                if not page.next_token:
                    break  # reached this round's deltaLink
        if ch.caught_up() and not account["backfill_done"]:
            store.update_account(conn, account_id, backfill_done=True)
            conn.commit()
    except BudgetExhausted:
        conn.rollback()
        stats["budget_exhausted"] = True  # resumes from the saved links next cycle


def sync_account(ctx: UserCtx, account_id: int) -> dict:
    """One sync cycle for one account. Returns counters."""
    stats = {"stored": 0, "labels": 0, "deleted": 0, "phase": ""}
    with db.user_session(ctx) as conn:
        account = store.get_account(conn, account_id)
        if account is None or account["status"] not in ("active", "error"):
            return stats
        try:
            if account["provider"] == "outlook":
                ch = open_channel(ctx, account, conn, Budget(MAX_PER_RUN))
                _pull_outlook(conn, ctx, account, ch, stats)
                try:
                    stats["retried"] = _retry_failures(conn, ctx, account, ch)
                except BudgetExhausted:
                    stats["retried"] = 0
            else:
                ch = open_channel(ctx, account)
                _pull_gmail(conn, ctx, account, ch, stats)
                stats["retried"] = _retry_failures(conn, ctx, account, ch)
            _persist_creds(conn, ctx, account_id, ch)
            store.update_account(conn, account_id, last_sync_at=_now(), last_error=None, status="active")
        except HttpError as e:
            conn.rollback()
            if e.resp.status in (403, 429) and "rate" in str(e).lower():
                log.warning("account %s: Gmail rate limit hit; resuming next cycle", account_id)
                store.update_account(conn, account_id, last_error="Gmail rate limit; backing off", last_sync_at=_now())
            else:
                log.exception("account %s sync failed", account_id)
                store.update_account(conn, account_id, last_error=str(e)[:3900], status="error", last_sync_at=_now())
        except RateLimited as e:
            conn.rollback()
            log.warning("account %s: %s; resuming next cycle", account_id, e)
            store.update_account(conn, account_id, last_error="Microsoft Graph throttling; backing off",
                                 last_sync_at=_now())
        except ReauthRequired as e:
            conn.rollback()
            log.warning("account %s: %s", account_id, e)
            store.update_account(conn, account_id, last_error=str(e), status="reauth", last_sync_at=_now())
        except CursorExpired:
            log.warning("account %s: history cursor expired, rescanning", account_id)
            store.update_account(conn, account_id, sync_cursor=None, backfill_done=False, backfill_token=None)
        except Exception as e:
            log.exception("account %s sync failed", account_id)
            conn.rollback()
            msg = str(e)[:3900]
            status = "reauth" if "invalid_grant" in msg else "error"
            store.update_account(conn, account_id, last_error=msg, status=status, last_sync_at=_now())
    return stats


def verify_account(ctx: UserCtx, account_id: int) -> dict:
    """Compare what the provider lists for the backfill window with what is stored (lists ids only; cheap)."""
    s = settings()
    with db.user_session(ctx) as conn:
        account = store.get_account(conn, account_id)
        ch = open_channel(ctx, account, conn)
        listed, token = [], None
        while True:
            page = ch.list_page(s.backfill_days, token)
            listed.extend(page.ids)
            token = page.next_token
            if not token:
                break
        stored = store.existing_ids(conn, account_id, listed)
        missing = [i for i in listed if i not in stored]
        out = {"address": account["address"], "window_days": s.backfill_days, f"in_{account['provider']}": len(listed),
               "stored": len(stored), "missing": len(missing), **store.failure_summary(conn, account_id)}
        _persist_creds(conn, ctx, account_id, ch)
        return out


def rescan_account(ctx: UserCtx, account_id: int) -> None:
    """Re-run the backfill: already-stored messages are skipped, missing ones fetched. Incremental cursor is kept."""
    with db.user_session(ctx) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sync_failures WHERE account_id = :1", [account_id])
        account = store.get_account(conn, account_id)
        if account and account["provider"] == "outlook":
            # restart each folder's window-filtered delta round (keeps resolved folder ids); stored mail is skipped
            state = {k: v for k, v in (account["sync_state"] or {}).items() if k != "sync"}
            store.update_account(conn, account_id, backfill_done=False, sync_state=state)
        else:
            store.update_account(conn, account_id, backfill_done=False, backfill_token=None)


def embed_user(ctx: UserCtx, max_batches: int = 5) -> int:
    s = settings()
    total = 0
    for _ in range(max_batches):
        with db.user_session(ctx) as conn:
            n = store.embed_pending(conn, s.embed_batch)
        total += n
        if n < s.embed_batch:
            break
    return total


def active_accounts() -> list[tuple[UserCtx, int]]:
    with db.system_session() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT a.tenant_id, a.user_id, u.email, a.id FROM accounts a JOIN users u ON u.id = a.user_id
                        WHERE a.status IN ('active','error') ORDER BY a.id""")
        return [(UserCtx(r[0], r[1], r[2]), r[3]) for r in cur]


def users_with_accounts() -> list[UserCtx]:
    seen, out = set(), []
    for ctx, _ in active_accounts():
        if ctx.user_id not in seen:
            seen.add(ctx.user_id)
            out.append(ctx)
    return out


_sender_refresh: dict[int, float] = {}
SENDER_REFRESH_SECONDS = 1800


def refresh_senders(ctx: UserCtx, force: bool = False) -> int | None:
    now = time.monotonic()
    if not force and now - _sender_refresh.get(ctx.user_id, -1e9) < SENDER_REFRESH_SECONDS:
        return None
    from . import senders
    with db.user_session(ctx) as conn:
        n = senders.refresh(conn)
    _sender_refresh[ctx.user_id] = now
    return n


def run_once(triage_enabled: bool = True) -> None:
    for ctx, account_id in active_accounts():
        stats = sync_account(ctx, account_id)
        log.info("user %s account %s: %s", ctx.user_id, account_id, stats)
    for ctx in users_with_accounts():
        if triage_enabled:
            try:
                from . import triage
                fast = triage.fast_pass(ctx)
                if fast:
                    log.info("user %s: fast pass settled %s (spam / one-time codes)", ctx.user_id, fast)
            except Exception:
                log.exception("user %s: fast pass failed", ctx.user_id)
        n = embed_user(ctx)
        if n:
            log.info("user %s: embedded %s chunks", ctx.user_id, n)
        if not triage_enabled:
            continue
        try:
            n = refresh_senders(ctx)
            if n is not None:
                log.info("user %s: sender stats refreshed (%s senders)", ctx.user_id, n)
            from . import triage
            counts = triage.triage_user(ctx)
            if any(counts.values()):
                log.info("user %s: triage %s", ctx.user_id, counts)
            scrubbed = triage.scrub_expired(ctx)
            if scrubbed:
                log.info("user %s: scrubbed %s expired codes/links", ctx.user_id, scrubbed)
        except Exception:
            log.exception("user %s: triage step failed", ctx.user_id)
        try:  # rule suggestions from reviewed decisions: at most once a day (no model calls)
            from . import rules
            with db.user_session(ctx) as conn:
                res = rules.refresh_suggestions(conn, key=ctx.user_id)
            if res and any(res.values()):
                log.info("user %s: rule suggestions %s", ctx.user_id, res)
        except Exception as e:
            log.warning("user %s: rule suggestions skipped: %s", ctx.user_id, str(e)[:200])


def run_forever() -> None:
    s = settings()
    stopping = False

    def _stop(*_):
        nonlocal stopping
        stopping = True
        log.info("stopping after this cycle")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started, polling every %ss", s.poll_seconds)
    try:  # re-check open decisions against today's rules (spam, impersonation, one-time codes)
        from . import triage
        for ctx in users_with_accounts():
            log.info("user %s: refresh %s", ctx.user_id, triage.refresh(ctx))
    except Exception:
        log.exception("refresh failed")
    while not stopping:
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            log.exception("cycle failed")
        while not stopping and time.monotonic() - started < s.poll_seconds:
            time.sleep(1)
