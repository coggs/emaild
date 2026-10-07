from contextlib import contextmanager

import pytest
from jinja2 import Environment, FileSystemLoader

from emaild import brief, db, recommend, telegram

ONE_CLICK_POST = "List-Unsubscribe=One-Click"


# ---------- List-Unsubscribe parsing ----------

def test_parse_one_click():
    r = recommend.parse_list_unsubscribe("<https://ex.com/u?id=1>, <mailto:u@ex.com?subject=unsub>", ONE_CLICK_POST)
    assert r["method"] == "one_click" and r["target"] == "https://ex.com/u?id=1"
    assert r["mailto"] == ["mailto:u@ex.com?subject=unsub"]


def test_parse_https_without_post_header_is_manual_url():
    r = recommend.parse_list_unsubscribe("<https://ex.com/u?id=1>", None)
    assert r["method"] == "url" and r["target"] == "https://ex.com/u?id=1"
    assert recommend.parse_list_unsubscribe("<https://ex.com/u>", "something-else")["method"] == "url"


def test_parse_mailto_only_and_both_order():
    assert recommend.parse_list_unsubscribe("<mailto:leave@list.org>")["method"] == "mailto"
    r = recommend.parse_list_unsubscribe("<mailto:leave@list.org>, <https://list.org/leave>", ONE_CLICK_POST)
    assert r["method"] == "one_click" and r["target"] == "https://list.org/leave"   # https preferred over mailto


def test_parse_http_is_never_one_click_and_folded_whitespace():
    r = recommend.parse_list_unsubscribe("<http://ex.com/u>", ONE_CLICK_POST)
    assert r["method"] == "url" and r["target"] == "http://ex.com/u"
    r = recommend.parse_list_unsubscribe("<https://ex.com/u?a=1\r\n &b=2>", ONE_CLICK_POST)
    assert r["target"] == "https://ex.com/u?a=1&b=2"


@pytest.mark.parametrize("header", ["", None, "garbage", "<javascript:alert(1)>", "<ftp://x/y>", "<https://>",
                                    "<mailto:nobody>"])
def test_parse_malformed(header):
    r = recommend.parse_list_unsubscribe(header, ONE_CLICK_POST)
    assert r["method"] is None and r["target"] is None


# ---------- one-click POST ----------

@pytest.fixture
def posts(monkeypatch):
    calls = []
    monkeypatch.setattr(recommend, "_resolve", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(recommend, "_post", lambda url: calls.append(url) or (200, None))
    return calls


def test_one_click_refuses_http_and_mailto(posts):
    assert recommend.one_click_unsubscribe("http://ex.com/u")[0] is False
    assert recommend.one_click_unsubscribe("mailto:u@ex.com")[0] is False
    assert posts == []


def test_one_click_refuses_private_hosts(posts, monkeypatch):
    for url in ("https://192.168.1.1/u", "https://localhost/u", "https://nas.local/u", "https://[::1]/u"):
        assert recommend.one_click_unsubscribe(url)[0] is False
    monkeypatch.setattr(recommend, "_resolve", lambda host: ["10.0.0.5"])     # public name, private address
    assert recommend.one_click_unsubscribe("https://evil.example/u")[0] is False
    assert posts == []


def test_one_click_posts_and_follows_only_https_redirects(posts, monkeypatch):
    ok, detail = recommend.one_click_unsubscribe("https://ex.com/u?id=1")
    assert ok and posts == ["https://ex.com/u?id=1"] and "200" in detail
    replies = iter([(302, "/done"), (200, None)])
    monkeypatch.setattr(recommend, "_post", lambda url: posts.append(url) or next(replies))
    assert recommend.one_click_unsubscribe("https://ex.com/u")[0] and posts[-1] == "https://ex.com/done"
    monkeypatch.setattr(recommend, "_post", lambda url: posts.append(url) or (302, "http://ex.com/plain"))
    n = len(posts)
    ok, detail = recommend.one_click_unsubscribe("https://ex.com/u")
    assert not ok and "https" in detail and len(posts) == n + 1


def test_post_sends_rfc8058_body(monkeypatch):
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen.update(method=request.method, body=request.content, ctype=request.headers["content-type"])
        return httpx.Response(200)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert recommend._post("https://ex.com/u") == (200, None)
    assert seen == {"method": "POST", "body": b"List-Unsubscribe=One-Click",
                    "ctype": "application/x-www-form-urlencoded"}


# ---------- follow-up heuristic ----------

@pytest.mark.parametrize("text,expected", [
    ("Are you free on Saturday?", True),
    ("Here's the invoice. Let me know if anything's wrong.", True),
    ("Could you send the signed form back", True),
    ("Any update on the quote", True),
    ("Please confirm the booking for 10 people", True),
    ("Thanks, all sorted. See you then.", False),
    ("Report attached: https://ex.com/r?id=4&x=1", False),     # '?' in a URL isn't a question
    ("", False),
])
def test_expects_reply(text, expected):
    assert recommend.expects_reply(text) is expected


# ---------- brief ----------

def test_brief_renders_waiting_on_others():
    from test_brief_telegram import B
    b = {**B, "waiting_on_others": [{"item_id": 7, "thread_id": 1, "to": "Bob <b>", "to_addr": "bob@x",
                                     "subject": "Quote for the deck?", "sent_at": "2026-10-01 01:00",
                                     "days_waiting": 6}], "unsub_suggestions": 4}
    txt = brief.render_telegram(b, "summary")
    assert "Waiting on others" in txt and "Bob &lt;b&gt;" in txt and "6 days" in txt and "Quote for the deck?" in txt
    assert "4 lists you could unsubscribe from" in txt
    assert "Quote for the deck?" not in brief.render_telegram(b, "minimal")
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    out = env.get_template("brief.html").render(b={"id": 1, "created_at": "x", **b}, page="brief", waiting=0)
    assert "Waiting on others" in out and "Bob &lt;b&gt;" in out and "/recommendations" in out


# ---------- templates ----------

UNSUBS = [{"id": 11, "sender_addr": "news@shop.com", "sender_name": "Shop <News>", "count": 12, "archived_share": 0.9,
           "last_received": "2026-10-05 10:00", "method": "one_click", "target": "https://shop.com/u",
           "reason": "12 emails in 60 days", "status": "suggested", "detail": None},
          {"id": 12, "sender_addr": "list@club.org", "sender_name": "Club list", "count": 5, "archived_share": 1.0,
           "last_received": "2026-10-04 10:00", "method": "url", "target": "https://club.org/leave?a=1&b=2",
           "reason": "5 emails", "status": "suggested", "detail": None}]
FOLLOWUPS = [{"item_id": 99, "thread_id": 3, "to": "Jane", "to_addr": "jane@x", "subject": "Can you confirm?",
              "sent_at": "2026-10-01 01:00", "days_waiting": 6}]


def test_recommendations_page_renders():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    out = env.get_template("recommendations.html").render(unsubs=UNSUBS, followups=FOLLOWUPS, page="recommendations",
                                                          waiting=0, days_min=3, days_max=21)
    assert 'hx-post="/recommendations/unsub/11"' in out and "Shop &lt;News&gt;" in out
    assert 'href="https://club.org/leave?a=1&amp;b=2"' in out and 'rel="noopener noreferrer"' in out
    assert 'hx-post="/recommendations/keep/12"' in out and "Keep getting these" in out
    assert "/recommendations/followup/99/dismiss" in out and "Can you confirm?" in out
    assert 'class="on">Recommendations' in out


def test_unsub_result_macro_escapes():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    m = env.get_template("_reco.html").module.unsub_result
    out = str(m(11, {"sender_addr": "a@<x>", "status": "failed", "detail": "HTTP 500 <b>", "link": None}))
    assert "a@&lt;x&gt;" in out and "&lt;b&gt;" in out


def test_status_fragment_reco_line():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    st = {"accounts": [], "embedding_backlog": 0}
    base = dict(status=st, tstats={"decisions": 0, "waiting_review": 0, "agreement": None}, needs=None, codes=[])
    out = env.get_template("status_fragment.html").render(**base, reco={"unsubs": 6, "followups": 2})
    assert "6 unsubscribe suggestions" in out and "2 follow-ups" in out and 'href="/recommendations"' in out
    out = env.get_template("status_fragment.html").render(**base, reco={"unsubs": 1, "followups": 0})
    assert "1 unsubscribe suggestion " in out and "follow-up" not in out
    assert "/recommendations" not in env.get_template("status_fragment.html").render(**base, reco={"unsubs": 0,
                                                                                                    "followups": 0})


# ---------- Telegram ----------

class FakeAPI:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": 500 + len(self.sent)}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


@pytest.fixture
def bot(monkeypatch):
    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    return telegram.Bot(FakeAPI())


def _cb(data):
    return {"id": "q", "data": data, "message": {"chat": {"id": 77}, "message_id": 10}}


def test_unsubs_command_and_buttons(bot, monkeypatch):
    monkeypatch.setattr(recommend, "suggestions", lambda conn, limit=20: UNSUBS[:limit])
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "/unsubs"}})
    assert len(bot.api.sent) == 2 and "Shop &lt;News&gt;" in bot.api.sent[0][1]
    datas = [b["callback_data"] for b in bot.api.sent[0][2]["inline_keyboard"][0]]
    assert datas == ["u:11", "k:11"] and all(len(d.encode()) <= 64 for d in datas)


def test_unsubscribe_callback(bot, monkeypatch):
    seen = {}

    def fake_act_id(conn, uid, action, actor="user"):
        seen.update(uid=uid, action=action, actor=actor)
        return {"sender_addr": "news@shop.com", "status": "done", "detail": "unsubscribed (HTTP 200)", "link": None}

    monkeypatch.setattr(recommend, "act_id", fake_act_id)
    bot.handle_callback(_cb("u:11"))
    assert seen == {"uid": 11, "action": "unsubscribe", "actor": "telegram"}
    assert ("answerCallbackQuery", {"callback_query_id": "q", "text": "🧹 Unsubscribed"}) in bot.api.calls
    assert bot.api.sent == []


def test_keep_callback_and_manual_link(bot, monkeypatch):
    results = {"dismiss": {"sender_addr": "list@club.org", "status": "dismissed", "detail": "kept", "link": None},
               "unsubscribe": {"sender_addr": "list@club.org", "status": "manual", "detail": "open the link",
                               "link": "https://club.org/leave?a=1&b=<2>"}}
    monkeypatch.setattr(recommend, "act_id", lambda conn, uid, action, actor="user": results[action])
    bot.handle_callback(_cb("k:12"))
    assert any(c[1].get("text") == "📌 Kept" for c in bot.api.calls)
    bot.handle_callback(_cb("u:12"))
    assert "https://club.org/leave?a=1&amp;b=&lt;2&gt;" in bot.api.sent[-1][1]


def test_followups_command_and_done_callback(bot, monkeypatch):
    monkeypatch.setattr(recommend, "followup_nudges", lambda conn, limit=10: FOLLOWUPS)
    done = []
    monkeypatch.setattr(recommend, "dismiss_nudge", lambda conn, item_id: done.append(item_id) or True)
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 77}, "text": "/followups"}})
    assert "Jane" in bot.api.sent[-1][1] and bot.api.sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == "f:99"
    bot.handle_callback(_cb("f:99"))
    assert done == [99]


def test_unknown_reco_id(bot, monkeypatch):
    monkeypatch.setattr(recommend, "act_id", lambda conn, uid, action, actor="user": None)
    bot.handle_callback(_cb("u:404"))
    assert ("answerCallbackQuery", {"callback_query_id": "q", "text": "Not found"}) in bot.api.calls


# ---------- act(): safety with a fake DB ----------

class FakeCursor:
    def __init__(self, flagged: bool, header: tuple | None):
        self.flagged, self.header, self.sql, self.rowcount = flagged, header, [], 1

    def execute(self, sql, binds=None):
        self.sql.append((" ".join(sql.split()), binds))
        self.last = sql

    def fetchone(self):
        if "FROM unsubscribes WHERE sender_addr" in self.last:
            return None
        if "COUNT(*)" in self.last:
            return (1 if self.flagged else 0,)
        if "list_unsubscribe_post" in self.last:
            return self.header
        return None


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


def test_act_refuses_flagged_sender_and_never_posts(posts, monkeypatch):
    monkeypatch.setattr(recommend.store, "audit", lambda *a, **k: None)
    cur = FakeCursor(flagged=True, header=("<https://ex.com/u>", ONE_CLICK_POST, None))
    res = recommend.act(FakeConn(cur), "Spam@Ex.com", "unsubscribe")
    assert res["status"] == "failed" and "spam" in res["detail"] and posts == []
    assert any("INSERT INTO unsubscribes" in s and b["status"] == "failed" for s, b in cur.sql)


def test_act_one_click_and_manual(posts, monkeypatch):
    monkeypatch.setattr(recommend.store, "audit", lambda *a, **k: None)
    res = recommend.act(FakeConn(FakeCursor(False, ("<https://ex.com/u>", ONE_CLICK_POST, "<l.ex.com>"))),
                        "news@ex.com", "unsubscribe")
    assert res["status"] == "done" and posts == ["https://ex.com/u"]
    res = recommend.act(FakeConn(FakeCursor(False, ("<mailto:leave@ex.com>", None, None))), "news@ex.com",
                        "unsubscribe")
    assert res["status"] == "manual" and res["link"] == "mailto:leave@ex.com" and len(posts) == 1
    with pytest.raises(ValueError):
        recommend.act(FakeConn(FakeCursor(False, None)), "x@y", "delete")
