from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from emaild import brief, db, telegram, triage

SYD = ZoneInfo("Australia/Sydney")

B = {"period": {"since": "2026-10-06 21:00", "until": "2026-10-07 21:00", "since_local": "Wed 07 Oct 08:00",
                "until_local": "Thu 08 Oct 08:00"},
     "received": 42, "per_account": {"a@x": 42}, "by_action": {"archive": 30, "keep": 10, "alert": 2},
     "alerts": [{"decision_id": 1, "item_id": 9, "sender": "Jane <script>", "subject": "Camping weekend",
                 "summary": "Asks if you can bring the tent", "received_at": "", "action": "alert",
                 "importance": "high", "category": "personal", "status": "proposed"}],
     "awaiting_reply": [], "important": [], "new_senders": [{"sender": "New Co", "addr": "n@c", "count": 1}],
     "waiting_review": 3, "account_problems": [], "overview": "Jane asked about camping."}


def test_render_telegram_detail_levels_and_escaping():
    full = brief.render_telegram(B, "summary", "https://emaild.example")
    assert "Camping weekend" in full and "&lt;script&gt;" in full and "<script>" not in full
    assert "30 filed as noise" in full and "/review" in full and "Wed 07 Oct 08:00" in full
    assert "Jane asked about camping." in full
    minimal = brief.render_telegram(B, "minimal")
    assert "Camping weekend" not in minimal and "Jane asked" not in minimal and "Jane" in minimal


def test_quiet_hours_wraps_midnight():
    q = "22:00-07:00"
    assert telegram.in_quiet_hours(datetime(2026, 10, 7, 23, 30), q)
    assert telegram.in_quiet_hours(datetime(2026, 10, 7, 6, 59), q)
    assert not telegram.in_quiet_hours(datetime(2026, 10, 7, 8, 0), q)
    assert telegram.in_quiet_hours(datetime(2026, 10, 7, 13, 0), "12:00-14:00")


def test_brief_due_once_per_day_after_time():
    days = "mon,tue,wed,thu,fri,sat,sun"
    wed_755 = datetime(2026, 10, 7, 7, 55, tzinfo=SYD)
    wed_801 = datetime(2026, 10, 7, 8, 1, tzinfo=SYD)
    assert not telegram.brief_due(wed_755, "08:00", days, None)
    assert telegram.brief_due(wed_801, "08:00", days, None)
    assert not telegram.brief_due(wed_801, "08:00", days, datetime(2026, 10, 7, 8, 0, tzinfo=SYD))
    assert telegram.brief_due(wed_801, "08:00", days, datetime(2026, 10, 6, 8, 0, tzinfo=SYD))
    assert not telegram.brief_due(wed_801, "08:00", "mon,tue", None)


def test_card_and_buttons():
    g = dict(decision_id=123456789, item_id=5, action="keep", importance="normal", category="notification",
             subject="Approaching pooled storage limit", sender="Cloud", summary="80% used", reasons="r",
             group_size=3, past_verdicts={"keep": 1, "archive": 1}, conflict=True)
    txt = telegram.render_card(g, "summary")
    assert "×3 similar" in txt and "both ways" in txt and "80% used" in txt
    assert "80% used" not in telegram.render_card(g, "minimal")
    kb = telegram.card_buttons(123456789, True, "https://x/item/5")
    datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert all(len(d.encode()) <= 64 for d in datas) and datas[0] == "v:a:123456789:r"
    assert kb["inline_keyboard"][1][0]["url"] == "https://x/item/5"


class FakeAPI:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": 500 + len(self.sent)}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


def test_unlinked_chat_gets_instructions(monkeypatch):
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: None)
    bot = telegram.Bot(FakeAPI())
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "what did jane say?"}})
    assert "isn't linked" in bot.api.sent[0][1]


def test_callback_applies_verdict_to_group_and_asks_why(monkeypatch):
    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    monkeypatch.setattr(triage, "pending_groups", lambda conn, limit=20: [{"decision_ids": [5, 6, 7]}])
    seen = {}

    def fake_review_many(conn, ids, verdict, corr, reason):
        seen.update(ids=ids, verdict=verdict, corr=corr)
        return {"reviewed": len(ids), "status": "corrected", "corrected": corr, "decision_ids": ids}

    monkeypatch.setattr(triage, "review_many", fake_review_many)
    bot = telegram.Bot(FakeAPI())
    bot.handle_callback({"id": "q", "data": "v:x:6", "message": {"chat": {"id": 77}, "message_id": 10}})
    assert seen == {"ids": [5, 6, 7], "verdict": "correct", "corr": {"action": "archive"}}
    assert any("Why?" in t for _, t, _ in bot.api.sent)
    prompt_key = next(iter(bot.reason_prompts))
    reasons = {}
    monkeypatch.setattr(triage, "set_reason", lambda conn, ids, r: reasons.update(ids=ids, r=r))
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 77}, "text": "never need these",
                                                   "reply_to_message": {"message_id": prompt_key[1]}}})
    assert reasons == {"ids": [5, 6, 7], "r": "never need these"}


def test_pages_render():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    out = env.get_template("brief.html").render(b={"id": 1, "created_at": "x", **B}, page="brief", waiting=3)
    assert "Camping weekend" in out and "30 filed as noise" in out and "&lt;script&gt;" in out
    out = env.get_template("telegram.html").render(link=None, page="telegram", waiting=0, configured=False,
                                                   brief_time="08:00", tz="Australia/Sydney",
                                                   levels=telegram.DETAIL_LEVELS)
    assert "EMAILD_TELEGRAM_TOKEN" in out and "Get a link code" in out
    st = {"accounts": [{"address": "a@x", "status": "active", "last_error": None, "backfill_done": True,
                        "items": 1234, "last_sync_at": None}], "embedding_backlog": 0}
    out = env.get_template("index.html").render(user="u", page="home", status=st, waiting=0,
                                                tstats={"decisions": 0, "waiting_review": 0, "agreement": None},
                                                needs={"alerts": B["alerts"], "awaiting_reply": []})
    assert "Needs attention" in out and "Camping weekend" in out
    assert "<td>1234</td>" in out and "built-in method" not in out


def test_protect_command(monkeypatch):
    from emaild import identities
    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    seen = {}
    monkeypatch.setattr(identities, "upsert",
                        lambda conn, name, allowed, kind, note=None: seen.update(name=name, allowed=allowed, kind=kind)
                        or {"name": name, "kind": kind, "allowed": identities.normalise_allowed(allowed)})
    monkeypatch.setattr(triage, "refresh", lambda ctx, days=30: {"checked": 9, "spam": 0, "suspicious": 2,
                                                                 "one_time": 0, "cleared": 0})
    bot = telegram.Bot(FakeAPI())
    bot.handle_update({"update_id": 3, "message": {"chat": {"id": 77}, "text": "/protectorg NHFC | northhillsfc.example.com"}})
    assert seen == {"name": "NHFC", "allowed": ["northhillsfc.example.com"], "kind": "org"}
    assert "@northhillsfc.example.com" in bot.api.sent[-1][1] and "2 suspicious" in bot.api.sent[-1][1]
    bot.handle_update({"update_id": 4, "message": {"chat": {"id": 77}, "text": "/protect Alex Rivera"}})
    assert "Use:" in bot.api.sent[-1][1]


def test_seen_button_and_commands_clear_needs(monkeypatch):
    from emaild import brief as brief_mod
    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    cleared = []
    monkeypatch.setattr(brief_mod, "dismiss", lambda conn, ids=None, days=30: cleared.append(ids) or 3)
    assert telegram.card_buttons(9, False, None, seen=True)["inline_keyboard"][-1][0]["callback_data"] == "s:9"
    bot = telegram.Bot(FakeAPI())
    bot.handle_callback({"id": "q", "data": "s:9", "message": {"chat": {"id": 77}, "message_id": 10}})
    bot.handle_update({"update_id": 3, "message": {"chat": {"id": 77}, "text": "/seen"}})
    assert cleared == [[9], None] and "Cleared 3" in bot.api.sent[-1][1]


def test_needs_rows_have_seen_button_and_clear_all():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    st = {"accounts": [], "embedding_backlog": 0}
    out = env.get_template("status_fragment.html").render(
        status=st, tstats={"decisions": 0, "waiting_review": 0, "agreement": None}, codes=[],
        needs={"alerts": [{"decision_id": 42, "item_id": 1, "sender": "A", "subject": "S", "summary": ""}],
               "awaiting_reply": []})
    assert 'hx-post="/needs/42/seen"' in out and 'hx-post="/needs/clear"' in out
