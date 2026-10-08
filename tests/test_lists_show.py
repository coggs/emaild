"""Numbered lists and /show N: list context (remember / resolve, the 30-minute bare-number rule, reply-to-list),
the summary card (deterministic key details, weak-summary regeneration), the whole email (chunking, truncation,
refusals, audit), callbacks with an ownership check, numbering on every list surface, CLI `show` and MCP
`show_email`. No DB, no model: fakes only. All names and domains are invented (example.*)."""
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import oracledb
import pytest

from emaild import brief, db, lists, query, recommend, show, telegram, triage
from emaild.llm.base import LLMResult


# ---------- fakes ----------

class ListDB:
    """A tiny in-memory list_context (plus canned item rows for show.load) behind a cursor-like API."""

    def __init__(self, items: dict | None = None, owned: set | None = None):
        self.rows: list[dict] = []          # {"ch", "ref", "ids", "age"}
        self.items = items or {}            # item id -> show.load row tuple
        self.owned = owned if owned is not None else set(self.items)
        self.calls: list = []
        self.age_for_new = 0.0

    def cursor(self):
        return _Cur(self)


class _Cur:
    def __init__(self, dbx):
        self.db, self._rows, self.rowcount = dbx, [], 1

    def execute(self, sql, binds=None):
        self.db.calls.append((sql, binds))
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO list_context"):
            self.db.rows.append({"ch": binds["ch"], "ref": binds["ref"], "ids": json.loads(binds["ids"]),
                                 "age": self.db.age_for_new})
            self._rows = []
        elif s.startswith("DELETE FROM list_context"):
            assert binds["keep"] == lists.KEEP and "FETCH FIRST :keep ROWS ONLY" in s
            self._rows = []
        elif "FROM list_context" in s:
            rows = [r for r in self.db.rows if r["ch"] == binds["ch"] and
                    ("ref" not in binds or r["ref"] == binds["ref"])]
            self._rows = [(json.dumps(rows[-1]["ids"]), rows[-1]["age"])] if rows else []
        elif "FROM items i LEFT JOIN decisions d" in s:
            r = self.db.items.get(binds["id"])
            self._rows = [r] if r else []
        elif s.startswith("SELECT 1 FROM items WHERE id = :id"):
            self._rows = [(1,)] if binds["id"] in self.db.owned else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Missing:
    """Before migration 016: list_context doesn't exist."""

    def cursor(self):
        return self

    def execute(self, sql, binds=None):
        raise oracledb.DatabaseError("ORA-00942: table or view does not exist")


def item_row(iid=501, subject="Hall booking for 14 Nov", summary="The hall is booked for Sat 14 Nov at 6pm; "
             "the $250 deposit is due Friday.", category="personal", source="llm", labels=("INBOX",),
             scrubbed=None, body=None, attachments=({"filename": "invoice.pdf"},)):
    body = body if body is not None else (
        "Hi Sam,\n\nThe hall is booked for Sat 14 Nov at 6:30pm. The deposit of $250.00 is due by 2026-10-16, "
        "the balance AUD 1,200 on the night.\nPay at https://pay.example.com/i/123?trk=abc or see "
        "https://www.venue.example.org/terms and https://maps.example.net/x and https://cdn.example.com/y\n"
        "> quoted old text\n\nThanks,\nAlex")
    return (iid, subject, "Alex Rivera", "alex@venue.example.org", datetime(2026, 10, 7, 23, 5, tzinfo=timezone.utc),
            body, body, json.dumps(list(attachments)), json.dumps(list(labels)), scrubbed, "snippet", 77,
            9001, summary, category, source, "keep")


class FakeAPI:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": 500 + len(self.sent)}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


def link(detail="summary"):
    return {"ctx": db.UserCtx(1, 1, "u@example.com"), "chat_id": 77, "detail": detail, "muted": False,
            "linked_at": None}


@pytest.fixture
def env(monkeypatch):
    """A bot whose every user_session yields the same ListDB."""
    store_ = ListDB({501: item_row(), 502: item_row(502, subject="Your code", category="one_time",
                                                    source="one_time")})
    lk = link()
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: lk)

    @contextmanager
    def sess(ctx):
        yield store_
    monkeypatch.setattr(db, "user_session", sess)
    monkeypatch.setattr(show.store, "audit", lambda conn, actor, action, target="", detail=None:
                        store_.calls.append(("AUDIT", (actor, action, target, detail))))
    bot = telegram.Bot(FakeAPI())
    return bot, store_, lk


def msg(text, reply_to=None, update_id=1):
    m = {"chat": {"id": 77}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": m}


# ---------- list context ----------

def test_remember_and_resolve_with_ref_vs_latest():
    c = ListDB()
    assert lists.remember(c, "telegram", "77:10", [11, 12, 13])
    assert lists.remember(c, "telegram", "77:20", [21, 22])
    assert lists.resolve(c, "telegram", 2) == 22                      # latest list
    assert lists.resolve(c, "telegram", 2, ref="77:10") == 12          # that list
    assert lists.resolve(c, "telegram", 3) is None                     # out of range
    assert lists.resolve(c, "telegram", 1, ref="77:99") is None        # no such list
    assert lists.resolve(c, "cli", 1) is None                          # other channel
    assert not lists.remember(c, "cli", "last", [])                    # nothing to remember
    with pytest.raises(ValueError):
        lists.remember(c, "sms", "x", [1])
    for sql, binds in c.calls:                                         # named binds only
        assert binds is None or isinstance(binds, dict)
        assert not re.search(r"(?<![\w:]):\d", sql)


def test_missing_table_is_graceful():
    assert lists.remember(Missing(), "telegram", "77:1", [1]) is False
    with pytest.raises(lists.Unavailable, match="emaild migrate"):
        lists.resolve(Missing(), "telegram", 1)


def test_age_limit_and_parse_request():
    c = ListDB()
    c.age_for_new = 45.0
    lists.remember(c, "telegram", "77:1", [5, 6])
    assert lists.resolve(c, "telegram", 1) == 5
    assert lists.resolve(c, "telegram", 1, max_age_minutes=lists.BARE_NUMBER_MINUTES) is None
    assert lists.parse_request("3") == {"n": 3, "mode": "card", "bare": True}
    assert lists.parse_request("show 3") == {"n": 3, "mode": "card", "bare": False}
    assert lists.parse_request("full 3")["mode"] == "full" and lists.parse_request("show 3 full")["mode"] == "full"
    assert lists.parse_request("thread 2")["mode"] == "thread"
    for t in ("show 3 emails from Sam", "3 things", "what about 3?", "", "show me"):
        assert lists.parse_request(t) is None, t


def test_renumber_citations_and_number():
    lines, ids = lists.renumber_citations(["a [email 301]", "b [email 302] and [email 301]"])
    assert lines == ["a [1]", "b [2] and [1]"] and ids == [301, 302]
    assert lists.number(["x", "y"]) == ["[1] x", "[2] y"]


# ---------- the summary card ----------

def test_key_details_are_deterministic_and_never_full_links():
    d = show.key_details(item_row()[5], ["invoice.pdf"])
    assert any("14 Nov" in x for x in d["dates"]) and any("2026-10-16" in x for x in d["dates"])
    assert "$250.00" in d["amounts"] and "AUD 1,200" in d["amounts"]
    assert d["links"] == {"count": 4, "domains": ["pay.example.com", "venue.example.org", "maps.example.net"]}
    assert d["attachments"] == ["invoice.pdf"]
    assert "trk=abc" not in json.dumps(d) and "/i/123" not in json.dumps(d)
    assert show.key_details("no links here http://plain.example.com", [])["links"]["count"] == 0   # https only


def test_card_render_escapes_and_unsafe_card_has_no_details():
    c = show.card(ListDB({501: item_row(subject="<b>Hall</b>")}), 501)
    out = show.render_card(c)
    assert "&lt;b&gt;Hall&lt;/b&gt;" in out and "<b>Hall" not in out
    assert "🔗 4 links (pay.example.com, venue.example.org, maps.example.net)" in out and "📎 invoice.pdf" in out
    assert re.search(r"Thu 08 Oct 2026 \d\d:05", c["date"])                     # local time zone, not UTC
    bad = show.card(ListDB({9: item_row(9, category="suspicious", source="security")}), 9)
    assert bad["unsafe"] == "suspicious" and bad["summary"] == "" and bad["details"]["links"]["count"] == 0
    assert "⚠️" in show.render_card(bad)
    assert show.card(ListDB(), 404) is None


class FakeRouter:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def chat(self, task, messages, *, policy="local_only", schema=None, conn=None, temperature=0.1):
        self.calls += 1
        assert "untrusted" in messages[0]["content"] and "<email>" in messages[1]["content"]
        return LLMResult(json.dumps({"summary": self.text}), "gemma", "ollama", True)


def test_weak_summary_is_regenerated_once_and_saved(monkeypatch):
    c = ListDB({501: item_row(summary="This email requires your attention because it is time-sensitive.")})
    monkeypatch.setattr(triage, "load_item", lambda conn, iid: {"id": iid, "subject": "Hall", "body": "x",
                                                                "sender_name": "A", "sender_addr": "a@x"})
    r = FakeRouter("The hall is booked for 14 Nov; a $250 deposit is due Friday.")
    card = show.card(c, 501, router=r)
    assert card["summary"].startswith("The hall is booked") and r.calls == 1
    upd = [(s, b) for s, b in c.calls if s.startswith("UPDATE decisions SET summary")]
    assert upd and upd[0][1]["id"] == 9001
    c2 = ListDB({501: item_row()})                                     # a good summary: no model call
    r2 = FakeRouter("unused")
    assert show.card(c2, 501, router=r2)["summary"].startswith("The hall is booked") and r2.calls == 0


# ---------- the whole email ----------

def test_clean_text_and_chunking_with_truncation():
    t = show.clean_text("Hello   there\r\n\r\n\r\n\r\nBody <b>&</b>\n> quoted\nOn Mon, Sam wrote:\nold stuff")
    assert t == "Hello there\n\nBody <b>&</b>"
    parts, trunc = show.chunk("short", 3500, 3)
    assert parts == ["short"] and not trunc
    long_ = "\n".join(f"line {i} " + "x&" * 40 for i in range(400))
    parts, trunc = show.chunk(long_, 3500, 3)
    assert trunc and len(parts) == 3 and all(len(__import__("html").escape(p)) <= 3500 for p in parts)
    parts, trunc = show.chunk("y" * 8000, 3500, 3)                    # one huge line is cut by characters
    assert not trunc and "".join(parts) == "y" * 8000


def test_full_refusals_and_audit():
    for row, why in ((item_row(category="spam"), "spam"), (item_row(category="suspicious"), "suspicious"),
                     (item_row(category="one_time", source="one_time"), "one_time"),
                     (item_row(category="personal", source="security"), "security"),
                     (item_row(scrubbed=datetime(2026, 10, 1)), "scrubbed"), (item_row(labels=("SPAM",)), "spam")):
        c = ListDB({501: row})
        res = show.full(c, 501, actor="telegram")
        assert res["refused"] == why and res["message"]
        assert not any(s.startswith("INSERT INTO audit_log") for s, _ in c.calls)
    c = ListDB({501: item_row()})
    res = show.full(c, 501, actor="telegram")
    assert res["parts"] and "quoted old text" not in res["parts"][0] and res["attachments"] == ["invoice.pdf"]
    audit = [b for s, b in c.calls if s.startswith("INSERT INTO audit_log")]
    assert audit and (audit[0]["actor"], audit[0]["action"], audit[0]["target"]) == ("telegram", "show_full", "501")
    assert show.full(ListDB(), 404, actor="telegram") is None


# ---------- Telegram: /show, /show N full, /thread, replies, callbacks ----------

def _remember(store_, ref, ids, age=0.0):
    store_.rows.append({"ch": "telegram", "ref": ref, "ids": ids, "age": age})


def test_show_card_and_full_in_telegram(env):
    bot, store_, lk = env
    _remember(store_, "77:400", [501, 502])
    bot.handle_update(msg("/show 1"))
    _, text, kb = bot.api.sent[-1]
    assert "Hall booking for 14 Nov" in text and "🗓" in text
    datas = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row if b.get("callback_data")]
    assert datas == ["e:f:501", "e:t:501"] and all(len(d.encode()) < 64 for d in datas)
    bot.handle_update(msg("/show 1 full"))
    _, text, _ = bot.api.sent[-1]
    assert text.startswith("📄 <b>Hall booking") and "$250.00" in text and "invoice.pdf" in text
    assert any(c[0] == "AUDIT" and c[1][1] == "show_full" for c in store_.calls)
    bot.handle_update(msg("/show 2 full"))                             # a one-time code: refused
    assert bot.api.sent[-1][1].startswith("🚫") and "sign-in code" in bot.api.sent[-1][1]
    bot.handle_update(msg("/show 9"))
    assert "That list has 2 emails" in bot.api.sent[-1][1]


def test_full_refused_at_minimal_detail(env):
    bot, store_, lk = env
    lk["detail"] = "minimal"
    _remember(store_, "77:400", [501])
    bot.handle_update(msg("/show 1 full"))
    assert "/detail summary" in bot.api.sent[-1][1] and "Hall" not in bot.api.sent[-1][1]
    assert not any(c[0] == "AUDIT" for c in store_.calls)
    bot.handle_update(msg("/show 1"))                                  # card: subject and sender only
    text, kb = bot.api.sent[-1][1], bot.api.sent[-1][2]
    assert "Hall booking" in text and "$250" not in text
    assert all(b.get("callback_data") != "e:f:501" for row in kb["inline_keyboard"] for b in row)


def test_full_split_into_three_messages_then_truncated(env, monkeypatch):
    bot, store_, lk = env
    store_.items[501] = item_row(body="\n".join(f"paragraph {i} " + "words " * 60 for i in range(200)))
    import dataclasses
    bot.s = dataclasses.replace(bot.s, public_url="https://emaild.example.com")
    _remember(store_, "77:400", [501])
    bot.handle_update(msg("/show 1 full"))
    texts = [t for _, t, _ in bot.api.sent]
    assert len(texts) == 3 and texts[1].startswith("<i>(2/3)</i>") and all(len(t) <= 4096 for t in texts)
    assert "truncated — open on dashboard: https://emaild.example.com/item/501" in texts[-1]


def test_reply_to_list_and_bare_number_rule(env, monkeypatch):
    bot, store_, lk = env
    opened, asked = [], []
    monkeypatch.setattr(bot, "send_card", lambda link, iid, reply_to=None: opened.append(("card", iid)))
    monkeypatch.setattr(bot, "send_full", lambda link, iid: opened.append(("full", iid)))
    monkeypatch.setattr(bot, "send_thread", lambda link, iid: opened.append(("thread", iid)))
    monkeypatch.setattr(bot, "answer_question", lambda link, q: asked.append(q))
    _remember(store_, "77:300", [301, 302, 303], age=120.0)           # an older list
    _remember(store_, "77:400", [401, 402], age=10.0)                 # the latest list
    bot.handle_update(msg("3", reply_to=300))
    bot.handle_update(msg("full 2", reply_to=300))
    bot.handle_update(msg("thread 1", reply_to=300))
    assert opened == [("card", 303), ("full", 302), ("thread", 301)]   # against THAT message's list
    bot.handle_update(msg("2"))                                        # bare, latest list is 10 min old
    assert opened[-1] == ("card", 402)
    store_.rows[-1]["age"] = 31.0
    bot.handle_update(msg("2"))                                        # too old: a normal question
    assert asked == ["2"] and opened[-1] == ("card", 402) and len(opened) == 4
    bot.handle_update(msg("/show 2"))                                  # the explicit command has no age limit
    assert opened[-1] == ("card", 402) and len(opened) == 5


def test_show_before_migration_says_migrate(monkeypatch):
    lk = link()
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: lk)

    @contextmanager
    def sess(ctx):
        yield Missing()
    monkeypatch.setattr(db, "user_session", sess)
    bot = telegram.Bot(FakeAPI())
    bot.handle_update(msg("/show 1"))
    assert "emaild migrate" in bot.api.sent[-1][1]


def test_email_callbacks_recheck_ownership(env, monkeypatch):
    bot, store_, lk = env
    store_.owned = {501}
    sent = []
    monkeypatch.setattr(bot, "send_full", lambda link, iid: sent.append(("full", iid)))
    monkeypatch.setattr(bot, "send_thread", lambda link, iid: sent.append(("thread", iid)))

    def cb(data):
        return {"id": "q", "data": data, "message": {"chat": {"id": 77}, "message_id": 9}}

    bot.handle_callback(cb("e:f:501"))
    bot.handle_callback(cb("e:t:501"))
    bot.handle_callback(cb("e:f:999"))                                 # not this user's (VPD finds nothing)
    assert sent == [("full", 501), ("thread", 501)]
    assert ("answerCallbackQuery", {"callback_query_id": "q", "text": "Not available"}) in bot.api.calls


def test_thread_renders_numbered_citations(env, monkeypatch):
    bot, store_, lk = env
    from emaild import projects
    monkeypatch.setattr(projects, "thread_status", lambda conn, item_id=None, query=None, router=None, today=None: {
        "subject": "Hall <booking>", "messages": 2, "last": {"date": "2026-10-07", "from": "Alex"}, "waiting": None,
        "state": "Booked.", "decisions": [{"text": "Hall booked", "item_id": 501}], "open_asks": [],
        "next_dates": [], "used_model": True, "projects": []})
    monkeypatch.setattr("emaild.llm.router.Router", lambda *a, **k: object())
    bot.send_thread(lk, 501)
    text = bot.api.sent[-1][1]
    assert "Hall &lt;booking&gt;" in text and "Hall booked [1]" in text
    assert store_.rows[-1]["ids"] == [501] and store_.rows[-1]["ref"] == f"77:{500 + len(bot.api.sent)}"


# ---------- numbering on every list surface ----------

def _list_res():
    return {"mode": "list", "interpreted": "list", "query": {"sort": "newest", "topic": "", "sender": None,
                                                             "sender_match": None},
            "items": [{"item_id": 11, "date": "2026-10-03", "sender": "A", "subject": "One", "summary": ""},
                      {"item_id": 12, "date": "2026-10-04", "sender": "B", "subject": "Two", "summary": ""}]}


def test_query_list_numbered_and_remembered_after_send(env, monkeypatch):
    bot, store_, lk = env
    monkeypatch.setattr(query, "run", lambda conn, q, router: _list_res())
    monkeypatch.setattr("emaild.llm.router.Router", lambda *a, **k: object())
    bot.handle_update(msg("last 2 emails"))
    text = bot.api.sent[-1][1]
    assert "[1] 3 Oct · One" in text and "[2] 4 Oct · Two" in text and "/show N" in text
    assert store_.rows[-1] == {"ch": "telegram", "ref": "77:501", "ids": [11, 12], "age": 0.0}
    ans = {"mode": "answer", "answer": "x [2]", "sources": [{"n": 2, "item_id": 8}, {"n": 1, "item_id": 7}]}
    assert telegram.query_ids(ans) == [7, 8]


def test_brief_uses_one_running_numbering():
    b = {"period": {"since": "2026-10-06 21:00", "until": "2026-10-07 21:00"}, "received": 3,
         "alerts": [{"item_id": 1, "sender": "A", "subject": "S1", "summary": ""}],
         "awaiting_reply": [{"item_id": 2, "sender": "B", "subject": "S2", "summary": ""}],
         "important": [{"item_id": 3, "sender": "C", "subject": "S3", "summary": ""}],
         "waiting_on_others": [{"item_id": 4, "to": "D", "subject": "S4", "days_waiting": 5}],
         "new_senders": [{"sender": "New Co", "addr": "n@example.com", "count": 1, "item_id": 5}]}
    ids: list = []
    out = brief.render_telegram(b, "summary", ids_out=ids)
    assert ids == [1, 2, 3, 4, 5]
    for n, s in ((1, "A"), (2, "B"), (3, "C")):
        assert f"[{n}] <b>{s}</b>" in out
    assert "[4] D · 5 days" in out and "[5] New Co" in out
    assert brief.number_map(b) == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}


def test_needs_and_followups_numbered(env, monkeypatch):
    bot, store_, lk = env
    monkeypatch.setattr(brief, "needs_you", lambda conn, days=3: {
        "alerts": [{"item_id": 31, "decision_id": 1, "subject": "Alert one", "sender": "A", "summary": ""}],
        "awaiting_reply": [{"item_id": 32, "decision_id": 2, "subject": "Reply two", "sender": "B", "summary": ""}]})
    bot.handle_update(msg("/needs"))
    text = bot.api.sent[-1][1]
    assert "[1] 🔔 <b>Alert one</b>" in text and "[2] ↩️ <b>Reply two</b>" in text
    assert store_.rows[-1]["ids"] == [31, 32]
    monkeypatch.setattr(recommend, "followup_nudges", lambda conn, limit=10: [
        {"item_id": 41, "to": "Sam Taylor", "subject": "Quote?", "days_waiting": 4},
        {"item_id": 42, "to": "Alex", "subject": "Venue?", "days_waiting": 6}])
    bot.handle_update(msg("/followups"))
    _, text, kb = bot.api.sent[-1]
    assert "[1] <b>Sam Taylor</b>" in text and "[2] <b>Alex</b>" in text and store_.rows[-1]["ids"] == [41, 42]
    assert [r[0]["callback_data"] for r in kb["inline_keyboard"]] == ["f:41", "f:42"]
    monkeypatch.setattr(recommend, "dismiss_nudge", lambda conn, iid: True)
    bot.handle_callback({"id": "q", "data": "f:41", "message": {"chat": {"id": 77}, "message_id": 9,
                                                                 "reply_markup": kb}})
    edit = [p for m, p in bot.api.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
    assert edit[0][0]["callback_data"] == "noop" and edit[1][0]["callback_data"] == "f:42"   # only that row


# ---------- CLI and MCP ----------

def test_cli_show_full_and_card(monkeypatch, capsys):
    from emaild import cli, users
    store_ = ListDB({501: item_row()})
    monkeypatch.setattr(users, "resolve", lambda *a: db.UserCtx(1, 1, "u@example.com"))

    @contextmanager
    def sess(ctx):
        yield store_
    monkeypatch.setattr(db, "user_session", sess)
    monkeypatch.setattr(show.store, "audit", lambda *a, **k: store_.calls.append(("AUDIT", a)))
    monkeypatch.setattr("emaild.llm.router.Router", lambda *a, **k: None)
    cli.main(["show", "1"])
    assert "no [1] in the last list" in capsys.readouterr().out
    lists.remember(store_, "cli", "last", [501])
    cli.main(["show", "1", "--full"])
    out = capsys.readouterr().out
    assert out.startswith("Hall booking for 14 Nov") and "attachments: invoice.pdf" in out and "$250.00" in out
    assert any(c[0] == "AUDIT" and c[1][1:3] == ("cli", "show_full") for c in store_.calls)
    cli.main(["show", "--item", "501"])
    out = capsys.readouterr().out
    assert "links: 4 (pay.example.com, venue.example.org, maps.example.net)" in out
    store_.items[502] = item_row(502, category="spam")
    cli.main(["show", "--item", "502", "--full"])
    assert "spam" in capsys.readouterr().out


def test_cli_needs_prints_indices_and_remembers(monkeypatch, capsys):
    from emaild import cli, users
    store_ = ListDB()
    monkeypatch.setattr(users, "resolve", lambda *a: db.UserCtx(1, 1, "u@example.com"))

    @contextmanager
    def sess(ctx):
        yield store_
    monkeypatch.setattr(db, "user_session", sess)
    monkeypatch.setattr(brief, "needs_you", lambda conn, days=3, limit=50: {
        "alerts": [{"item_id": 31, "decision_id": 7, "subject": "Alert one", "sender": "A", "received_at": "x"}],
        "awaiting_reply": []})
    cli.main(["needs"])
    out = capsys.readouterr().out
    assert "[1] x" in out and "(seen id 7)" in out and "emaild show N" in out
    assert store_.rows[-1] == {"ch": "cli", "ref": "last", "ids": [31], "age": 0.0}


def test_mcp_show_email_card_full_and_refusals(monkeypatch):
    from emaild import mcp_server
    store_ = ListDB({501: item_row(), 502: item_row(502, category="suspicious")})
    monkeypatch.setattr(mcp_server, "_ctx", lambda: db.UserCtx(1, 1, "u@example.com"))
    monkeypatch.setattr(mcp_server, "_r", lambda: None)

    @contextmanager
    def sess(ctx):
        yield store_
    monkeypatch.setattr(db, "user_session", sess)
    monkeypatch.setattr(show.store, "audit", lambda *a, **k: store_.calls.append(("AUDIT", a)))
    card = mcp_server.show_email(501)
    assert card["subject"].startswith("Hall") and card["details"]["links"]["domains"][0] == "pay.example.com"
    full = mcp_server.show_email(501, full=True)
    assert "$250.00" in full["text"] and full["attachments"] == ["invoice.pdf"] and not full["truncated"]
    bad = mcp_server.show_email(502, full=True)
    assert bad["refused"] == "suspicious" and "text" not in bad
    assert mcp_server.show_email(999) == {"error": "not found"}


def test_migration_016_shape():
    from pathlib import Path
    sql = Path("db/migrations/016_list_context.sql").read_text()
    stmts = db.split_script(sql)
    assert any(s.startswith("CREATE TABLE list_context") for s in stmts)
    assert "SYS_CONTEXT('EMAILD_CTX','USER_ID')" in sql and "VPD_USER_SCOPE" in sql and "'LIST_CONTEXT'" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON list_context TO email_app" in sql
    assert "ALTER TABLE tracker_items ADD (closed_reason" in sql and "ALTER TABLE accounts ADD (ms_tenant" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
