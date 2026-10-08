"""Delivery/order questions answered from trackers, orders aging out as "assumed delivered", reopening, clear-old,
Mark delivered, and the labels. No DB, no model: fakes only. All names and domains are invented (example.*)."""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest

from emaild import db, telegram, trackers
from test_trackers import LINK, NOW, TODAY, Conn, FakeAPI, assert_named_binds, compiled, statements, tracker


# ---------- routing ----------

@pytest.mark.parametrize("q,who,when", [
    ("when is my Acme Shop delivery due?", "Acme Shop", None),
    ("When will my parcel arrive", None, None),
    ("has my order shipped?", None, None),
    ("where's my package", None, None),
    ("any deliveries today?", None, "today"),
    ("what orders are pending?", None, None),
    ("when is my order from Acme Shop arriving", "Acme Shop", None),
    ("has the Acme Shop order been dispatched", "Acme Shop", None),
    ("when did the bike pump arrive?", "bike pump", None),
])
def test_delivery_questions_route(q, who, when):
    it = trackers.parse_intent(q)
    assert it and it["op"] == "delivery" and it["who"] == who and it["when"] == when, (q, it)


@pytest.mark.parametrize("q", [
    "when is the school concert", "when is my dentist appointment", "when will the order of service be printed",
    "has my payment gone through", "where's the invoice", "what orders did I place in August",
    "last 5 emails from Sam Taylor", "what did the accountant say?", "when is the delivery of the new curriculum",
])
def test_ordinary_questions_are_not_hijacked(q):
    it = trackers.parse_intent(q)
    assert it is None or it["op"] != "delivery", (q, it)


def test_existing_status_intent_unchanged_and_clear_old_intent():
    assert trackers.parse_intent("what's still in transit?") == {"op": "status", "kind": "orders"}
    assert trackers.parse_intent("close the old Acme Shop orders") == {"op": "clear_old", "ref": "Acme Shop"}
    assert trackers.parse_intent("clear old orders") == {"op": "clear_old", "ref": None}
    assert trackers.parse_intent("close the old ticket") is None


# ---------- answering from open orders ----------

def _row(iid, title, state, expected=None, changed=NOW - timedelta(days=2), email=None, retailer=None, tid=3):
    f = {}
    if expected:
        f["expected_date"] = expected
    if retailer:
        f["retailer"] = retailer
    return {"id": iid, "tracker_id": tid, "tracker": "", "kind": "orders", "item_key": title.lower(), "title": title,
            "fields": f, "state": state, "first_seen_at": None, "last_changed_at": changed, "last_heard_at": changed,
            "closed_at": None, "email_id": email, "closed_reason": None}


@pytest.fixture
def boards(monkeypatch):
    acme = tracker(tid=3, name="Acme Shop orders")
    other = tracker(tid=4, name="Example Books orders",
                    match={"senders": ["Example Books"], "sender_addrs": ["orders@books.example.net"]})
    rows = [_row(1, "Desk lamp", "shipped", "2026-10-12", email=101),
            _row(2, "Bike pump", "out_for_delivery", email=102),
            _row(3, "Novel", "ordered", "2026-10-09", email=103, tid=4)]
    monkeypatch.setattr(trackers, "active_trackers", lambda conn: [acme, other])
    monkeypatch.setattr(trackers, "items", lambda conn, tracker_id=None, state=None, include_closed=False,
                        limit=200: [r for r in rows if include_closed or not r["closed_at"]])
    monkeypatch.setattr(trackers, "_local_today", lambda: date(2026, 10, 9))
    return rows


def test_delivery_answer_open_only_sorted_and_filtered(boards):
    ans = trackers.delivery_answer(object())
    assert [i["tracker_item_id"] for i in ans["items"]] == [3, 1, 2]          # soonest expected first, undated last
    assert ans["items"][0]["line"].startswith("Novel: ordered · expected today")
    assert all("last update" in i["line"] for i in ans["items"])
    acme = trackers.delivery_answer(object(), who="Acme Shop")
    assert [i["email_id"] for i in acme["items"]] == [101, 102] and "from Acme Shop" in acme["title"]
    assert trackers.delivery_answer(object(), who="bike pump")["items"][0]["tracker_item_id"] == 2
    assert trackers.delivery_answer(object(), who="garden hose") is None      # nothing open: search the mail
    today = trackers.delivery_answer(object(), when="today")
    assert [i["tracker_item_id"] for i in today["items"]] == [3, 2]           # expected today / out for delivery
    boards[0]["closed_at"] = "2026-10-01"                                     # closed orders never answer
    assert 1 not in [i["tracker_item_id"] for i in trackers.delivery_answer(object())["items"]]


def test_no_orders_tracker_falls_back(monkeypatch):
    monkeypatch.setattr(trackers, "active_trackers", lambda conn: [tracker("service", name="Example services")])
    assert trackers.delivery_answer(object()) is None


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: LINK)

    @contextmanager
    def sess(ctx):
        yield object()
    monkeypatch.setattr(db, "user_session", sess)
    return telegram.Bot(FakeAPI())


def test_telegram_delivery_answer_numbered_with_buttons(bot, boards, monkeypatch):
    remembered = []
    monkeypatch.setattr(bot, "_remember", lambda link, msg, ids: remembered.append(ids))
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "when is my Acme Shop delivery due?"}})
    _, text, kb = bot.api.sent[-1]
    assert "📦 <b>2 open orders from Acme Shop" in text and "[1] Desk lamp: shipped" in text and "[2] Bike pump" in text
    assert remembered[-1] == [101, 102]
    datas = [r[0]["callback_data"] for r in kb["inline_keyboard"]]
    assert datas == ["ti:d:1", "ti:d:2"] and all(len(d.encode()) < 64 for d in datas)
    asked = []
    monkeypatch.setattr(bot, "answer_question", lambda link, q: asked.append(q))
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 77}, "text": "when did the garden hose arrive?"}})
    assert asked == ["when did the garden hose arrive?"]                     # past item, nothing open
    bot.handle_update({"update_id": 3, "message": {"chat": {"id": 77}, "text": "when is the school concert?"}})
    assert asked[-1] == "when is the school concert?"


def test_mark_delivered_callback(bot, monkeypatch):
    marked = []
    monkeypatch.setattr(trackers, "mark_item", lambda conn, iid, state="delivered", actor="user":
                        marked.append((iid, state, actor)) or {"ok": True, "title": "Desk lamp", "state": state,
                                                               "tracker_id": 3})
    kb = {"inline_keyboard": [[{"text": "a", "callback_data": "ti:d:1"}], [{"text": "b", "callback_data": "ti:d:2"}]]}
    bot.handle_callback({"id": "q", "data": "ti:d:1", "message": {"chat": {"id": 77}, "message_id": 9,
                                                                   "reply_markup": kb}})
    assert marked == [(1, "delivered", "telegram")]
    assert "Desk lamp marked delivered" in [p for m, p in bot.api.calls if m == "answerCallbackQuery"][-1]["text"]
    rows = [p for m, p in bot.api.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "b", "callback_data": "ti:d:2"}]]
    monkeypatch.setattr(trackers, "mark_item", lambda conn, iid, state="delivered", actor="user":
                        {"error": "Not found (or not yours)."})
    bot.handle_callback({"id": "q", "data": "ti:d:99", "message": {"chat": {"id": 77}, "message_id": 9}})
    assert [p for m, p in bot.api.calls if m == "answerCallbackQuery"][-1]["text"] == "Not found (or not yours)."


def test_mark_item_sql_checks_state_and_named_binds():
    comp = json.dumps(compiled())
    c = Conn(lambda s, b: [(5, "Desk lamp", "shipped", None, 3, comp)] if "FROM tracker_items ti" in s else [])
    res = trackers.mark_item(c, 5, "delivered", actor="mcp")
    assert res["ok"] and res["state"] == "delivered"
    upd = statements(c, "UPDATE tracker_items SET state")[0]
    assert upd[1]["reason"] == "marked" and "closed_at IS NULL" in upd[0]
    ev = statements(c, "INSERT INTO tracker_events")[0][1]
    assert ev["new"] == "delivered" and json.loads(ev["f"]) == {"by": "mcp"}
    assert_named_binds(c)
    assert "error" in trackers.mark_item(c, 5, "shipped")                     # not a finished state
    assert "error" in trackers.mark_item(Conn(), 404)                          # not found / not yours


# ---------- aging out ----------

def test_assumed_delivered_thresholds():
    sp = trackers.spec_for(compiled())
    assert trackers.assumed_delivered(sp, "shipped", NOW - timedelta(days=30), {}, NOW)
    assert not trackers.assumed_delivered(sp, "shipped", NOW - timedelta(days=29), {}, NOW)
    exp = {"expected_date": (NOW - timedelta(days=21)).strftime("%Y-%m-%d")}
    assert trackers.assumed_delivered(sp, "shipped", NOW - timedelta(days=25), exp, NOW)
    assert not trackers.assumed_delivered(sp, "shipped", NOW - timedelta(days=5), exp, NOW)   # news since
    assert not trackers.assumed_delivered(sp, "problem", NOW - timedelta(days=90), {}, NOW)    # needs the user
    assert not trackers.assumed_delivered(trackers.spec_for(compiled("service")), "down", NOW - timedelta(days=90),
                                          {}, NOW)
    assert trackers.ASSUME_AFTER_EXPECTED_DAYS == 21 and trackers.ASSUME_QUIET_DAYS == 30


def test_backfill_creates_old_order_closed_and_silent():
    t = tracker()
    ext = {"item_key": "123-456", "title": "Bike pump", "state": "shipped", "fields": {},
           "occurred_at": NOW - timedelta(days=31)}
    c = Conn()
    res = trackers.apply_extraction(c, t, 501, ext, NOW)
    ins = statements(c, "INSERT INTO tracker_items")[0][1]
    assert ins["closed"] == NOW and ins["reason"] == "assumed_delivered" and res["closed_reason"] == "assumed_delivered"
    assert res["notify"] is False and statements(c, "INSERT INTO tracker_events")[0][1]["nt"] is False
    assert_named_binds(c)
    c = Conn()                                                                # recent: stays open
    trackers.apply_extraction(c, t, 502, {**ext, "occurred_at": NOW - timedelta(days=3)}, NOW)
    ins = statements(c, "INSERT INTO tracker_items")[0][1]
    assert ins["closed"] is None and ins["reason"] is None
    c = Conn()                                                                # old, but expected in the future
    later = (NOW + timedelta(days=5)).strftime("%Y-%m-%d")
    trackers.apply_extraction(c, t, 503, {**ext, "fields": {"expected_date": later}}, NOW)
    assert statements(c, "INSERT INTO tracker_items")[0][1]["closed"] is None


def test_ongoing_auto_close_assumes_delivered_without_notifying():
    comp = json.dumps(compiled())
    old = NOW - timedelta(days=31)
    rows = [(1, "delivered", NOW - timedelta(days=9), comp, "{}", None),           # normal close
            (2, "shipped", old, comp, "{}", old),                                  # quiet 31 days
            (3, "shipped", NOW - timedelta(days=40), comp,                         # 21 days past expected
             json.dumps({"expected_date": (NOW - timedelta(days=22)).strftime("%Y-%m-%d")}), NOW - timedelta(days=25)),
            (4, "shipped", NOW - timedelta(days=40), comp, "{}", NOW - timedelta(days=2)),   # heard recently
            (5, "problem", old, comp, "{}", old)]                                   # needs the user
    c = Conn(lambda s, b: rows if "FROM tracker_items ti" in s else [])
    assert trackers.close_due(c, NOW) == 3
    assert statements(c, "UPDATE tracker_items SET closed_at = SYSTIMESTAMP WHERE")[0][1] == {"id": 1}
    aged = [b for s, b in statements(c, "closed_reason = :r")]
    assert aged == [{"id": 2, "r": "assumed_delivered"}, {"id": 3, "r": "assumed_delivered"}]
    assert not statements(c, "INSERT INTO tracker_events")                     # aged-out closes never notify
    assert_named_binds(c)


def test_later_email_reopens_assumed_delivered_item():
    t = tracker()
    row = (9, "123-456", "Bike pump", "shipped", 1, NOW - timedelta(days=40), NOW - timedelta(days=5), "{}",
           "assumed_delivered")
    c = Conn(lambda s, b: [row] if "FROM tracker_items" in s else [])
    res = trackers.apply_extraction(c, t, 601, {"item_key": "123-456", "title": "Bike pump", "state": "shipped",
                                                "fields": {}, "occurred_at": NOW - timedelta(hours=2)}, NOW)
    assert res["outcome"] == "repeat" and res["reopened"]
    sql = statements(c, "UPDATE tracker_items")[0][0]
    assert "closed_at = NULL, closed_reason = NULL" in sql
    c = Conn(lambda s, b: [row] if "FROM tracker_items" in s else [])
    res = trackers.apply_extraction(c, t, 602, {"item_key": "123-456", "title": "Bike pump", "state": "delivered",
                                                "fields": {}, "occurred_at": NOW - timedelta(hours=1)}, NOW)
    upd = statements(c, "UPDATE tracker_items")[0][1]
    assert res["outcome"] == "change" and upd["closed"] is None and upd["reason"] is None   # applied normally
    confirmed = (9, "123-456", "Bike pump", "delivered", 3, NOW - timedelta(days=40), NOW - timedelta(days=30), "{}",
                 None)
    c = Conn(lambda s, b: [confirmed] if "FROM tracker_items" in s else [])
    trackers.apply_extraction(c, t, 603, {"item_key": "123-456", "title": "Bike pump", "state": "delivered",
                                          "fields": {}, "occurred_at": NOW}, NOW)
    assert "closed_at = NULL" not in statements(c, "UPDATE tracker_items")[0][0]   # a real delivery stays closed


def test_clear_old_sql_and_errors(monkeypatch):
    monkeypatch.setattr(trackers, "get", lambda conn, tid: tracker(tid=tid) if tid == 3 else
                        tracker("service", tid=tid))
    c = Conn()
    assert trackers.clear_old(c, 3, 21, actor="cli") == 1
    sql, b = statements(c, "UPDATE tracker_items SET closed_at")[0]
    assert b["days"] == 21 and b["r"] == "assumed_delivered" and "closed_at IS NULL" in sql and b["tid"] == 3
    assert_named_binds(c)
    assert trackers.clear_old(Conn(), 4) == 0                                  # not an orders tracker


def test_boards_label_assumed_delivered_apart_from_confirmed():
    t = tracker()
    closed = {**_row(1, "Desk lamp", "shipped"), "closed_at": "2026-10-01 00:00", "closed_reason": "assumed_delivered"}
    d = trackers.decorate(t, closed, NOW, TODAY)
    assert d["label"] == "assumed delivered (no confirmation email)" and d["tone"] == "info" and not d["stalled"]
    real = trackers.decorate(t, {**_row(2, "Pump", "delivered"), "closed_at": "2026-10-01 00:00"}, NOW, TODAY)
    assert real["label"] == "delivered" and real["tone"] == "good"
    marked = trackers.decorate(t, {**_row(3, "Mat", "delivered"), "closed_at": "x", "closed_reason": "marked"}, NOW,
                               TODAY)
    assert marked["label"] == "delivered (marked by you)"
    board = {"tracker": t, "icon": "📦", "open": [], "closed": [d, real]}
    text, _ = telegram.render_boards([board])
    assert "2 finished (1 assumed delivered, no confirmation email)" in text


def test_tracker_clear_old_command_and_free_text(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(trackers, "find_tracker", lambda conn, ref: tracker(tid=3) if ref in ("3", "Acme Shop")
                        else None)
    monkeypatch.setattr(trackers, "list_trackers", lambda conn: [tracker(tid=3), tracker("service", tid=5)])
    monkeypatch.setattr(trackers, "clear_old", lambda conn, tid, days=21, actor="user":
                        calls.append((tid, days)) or 2)
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "/tracker 3 clear old"}})
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 77}, "text": "/tracker clear old 3 30"}})
    bot.handle_update({"update_id": 3, "message": {"chat": {"id": 77}, "text": "close the old Acme Shop orders"}})
    bot.handle_update({"update_id": 4, "message": {"chat": {"id": 77}, "text": "clear old orders"}})
    bot.handle_update({"update_id": 5, "message": {"chat": {"id": 77}, "text": "/tracker clear old"}})
    assert calls == [(3, 21), (3, 30), (3, 21), (3, 21), (3, 21)]          # services are never touched
    assert "Closed 2 orders" in bot.api.sent[-1][1]


def test_web_board_has_mark_delivered_and_clear_old():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    t = {**tracker(), "readback": "rb", "last_event_at": None}
    it = trackers.decorate(t, _row(7, "Desk lamp", "shipped", email=101), NOW, TODAY)
    gone = trackers.decorate(t, {**_row(8, "Mat", "shipped"), "closed_at": "2026-10-01 00:00",
                                 "closed_reason": "assumed_delivered"}, NOW, TODAY)
    out = env.get_template("_trackers.html").module.board({"tracker": t, "icon": "📦", "open": [it],
                                                           "closed": [gone]})
    out = str(out)
    assert 'hx-post="/trackers/items/7/delivered"' in out and 'hx-post="/trackers/3/clear-old"' in out
    assert "assumed delivered (no confirmation email)" in out and "/trackers/items/8/delivered" not in out


def test_mcp_close_tracker_item(monkeypatch):
    from emaild import mcp_server
    monkeypatch.setattr(mcp_server, "_ctx", lambda: db.UserCtx(1, 1, "u@example.com"))

    @contextmanager
    def sess(ctx):
        yield object()
    monkeypatch.setattr(db, "user_session", sess)
    monkeypatch.setattr(trackers, "mark_item", lambda conn, iid, state="delivered", actor="user":
                        {"ok": True, "state": state, "actor": actor, "id": iid})
    assert mcp_server.close_tracker_item(5) == {"ok": True, "state": "delivered", "actor": "mcp", "id": 5}
