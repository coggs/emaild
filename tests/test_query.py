import json
from contextlib import contextmanager
from datetime import date, datetime

from jinja2 import Environment, FileSystemLoader

from emaild import ask as ask_mod
from emaild import db, query, telegram
from emaild.llm.base import LLMResult
from emaild.search import Filters, Hit

TODAY = date(2026, 10, 7)  # a Wednesday


# ---------- quick_parse ----------

def test_quick_parse_list_shapes_are_confident():
    q = query.quick_parse("show me the last 5 emails from Sam Taylor", TODAY)
    assert q.confident and q.mode == "list" and q.sender == "Sam Taylor" and q.limit == 5 and q.sort == "newest"
    q = query.quick_parse("anything from Riverside Rovers this week", TODAY)
    assert q.confident and q.mode == "list" and q.sender == "Riverside Rovers"
    assert q.after == date(2026, 10, 5) and q.before is None and q.sort == "newest"
    q = query.quick_parse("last five emails from Telstra", TODAY)
    assert q.confident and q.limit == 5 and q.sender == "Telstra"
    q = query.quick_parse("recent messages from jbhifi about gift cards", TODAY)
    assert q.confident and q.topic == "gift cards" and q.sender == "jbhifi"


def test_quick_parse_dates():
    q = query.quick_parse("emails from Matt today", TODAY)
    assert (q.after, q.before) == (TODAY, None)
    q = query.quick_parse("messages from Sarah yesterday", TODAY)
    assert (q.after, q.before) == (date(2026, 10, 6), TODAY)
    q = query.quick_parse("emails from the school last week", TODAY)
    assert (q.after, q.before) == (date(2026, 9, 28), date(2026, 10, 5)) and q.sender == "school"
    q = query.quick_parse("emails from Matt in August", TODAY)
    assert (q.after, q.before) == (date(2026, 8, 1), date(2026, 9, 1))
    q = query.quick_parse("emails from Matt in December", TODAY)   # a future month means last year
    assert (q.after, q.before) == (date(2025, 12, 1), date(2026, 1, 1))
    q = query.quick_parse("emails from May Smith", TODAY)           # a name, not the month
    assert q.sender == "May Smith" and q.after is None


def test_quick_parse_questions_are_best_effort_only():
    q = query.quick_parse("What are the latest perks from JB Hi-Fi?", TODAY)
    assert not q.confident and q.mode == "answer" and q.sender == "JB Hi-Fi" and q.sort == "newest"
    assert "perks" in q.topic
    q = query.quick_parse("what did the accountant say about BAS in August", TODAY)
    assert not q.confident and q.mode == "answer" and q.topic == "BAS"
    assert (q.after, q.before) == (date(2026, 8, 1), date(2026, 9, 1))
    q = query.quick_parse("when is my car service booked?", TODAY)
    assert not q.confident and q.topic == "when is my car service booked?" and q.sort == "relevant"


# ---------- validation ----------

def test_validate_cleans_model_output():
    q = query.validate({"mode": "weird", "topic": "x", "sender": "", "after": "2026-13-40", "before": "nope",
                        "limit": 999, "sort": "?"}, "q", TODAY)
    assert q.mode == "answer" and q.after is None and q.before is None and q.limit == 25 and q.sort == "relevant"
    q = query.validate({"mode": "list", "topic": "", "after": "2026-09-30", "before": "2026-09-01", "limit": 0},
                       "q", TODAY)
    assert (q.after, q.before) == (date(2026, 9, 1), date(2026, 9, 30)) and q.limit == 5 and q.sort == "newest"
    q = query.validate({"mode": "list", "after": "2026-09-03", "before": "2026-09-03"}, "q", TODAY)
    assert q.before == date(2026, 9, 4)
    q = query.validate({"mode": "list", "after": "2027-01-01", "sender": "anyone", "limit": "abc"}, "q", TODAY)
    assert q.after is None and q.sender is None and q.limit == 5
    q = query.validate({"mode": "answer"}, "who won the raffle?", TODAY)
    assert q.topic == "who won the raffle?"


# ---------- understand ----------

class FakeRouter:
    class s:
        privacy_default = "local_only"

    def __init__(self, text=None, fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def chat(self, task, messages, *, policy="local_only", schema=None, conn=None, temperature=0.1):
        self.calls.append(dict(task=task, messages=messages, schema=schema, temperature=temperature))
        if self.fail:
            raise RuntimeError("ollama down")
        return LLMResult(self.text, "gemma4", "ollama", True)


def test_understand_uses_model_for_questions():
    r = FakeRouter(json.dumps({"mode": "answer", "topic": "perks offers deals", "sender": "JB Hi-Fi", "after": "",
                               "before": "", "limit": 8, "sort": "newest"}))
    q = query.understand("What are the latest perks from JB Hi-Fi?", r, today=TODAY, tz="Australia/Sydney")
    assert q.source == "llm" and q.sender == "JB Hi-Fi" and q.sort == "newest" and q.topic == "perks offers deals"
    call = r.calls[0]
    assert call["temperature"] == 0 and call["schema"] is query.SCHEMA and call["task"] == "query"
    assert "2026-10-07" in call["messages"][0]["content"]
    assert call["messages"][1]["content"] == "What are the latest perks from JB Hi-Fi?"


def test_understand_skips_model_when_confident():
    r = FakeRouter("{}")
    q = query.understand("last 5 emails from Sam Taylor", r, today=TODAY, tz="Australia/Sydney")
    assert q.source == "quick" and r.calls == []


def test_understand_falls_back_on_failure_or_junk():
    for r in (FakeRouter(fail=True), FakeRouter("sorry, I can't"), FakeRouter("[1, 2]")):
        q = query.understand("What are the latest perks from JB Hi-Fi?", r, today=TODAY, tz="Australia/Sydney")
        assert q.source == "fallback" and q.sender == "JB Hi-Fi" and q.mode == "answer"
    q = query.understand("when is the car service?", FakeRouter(fail=True), today=TODAY, tz="Australia/Sydney")
    assert q.mode == "answer" and q.topic == "when is the car service?" and q.sort == "relevant"


# ---------- sender resolution ----------

def test_sender_normalisation():
    assert query.norm("JB Hi-Fi") == "jbhifi"
    rows = [("JB Hi-Fi", "offers@email.jbhifi.com.au", 40), (None, "noreply@jbhifi.com.au", 3),
            ("JB Hi-Fi Fan Club", "fans@example.com", 1)]
    m = query.pick_sender("JB Hi-Fi", rows)
    assert m["label"] == "JB Hi-Fi" and m["addrs"][0] == "offers@email.jbhifi.com.au"
    assert "noreply@jbhifi.com.au" in m["addrs"]
    m = query.pick_sender("Sam Taylor", [("Taylor, Sam", "matt@firm.com.au", 5), ("Matt Smith", "ms@x.com", 9)])
    assert m["addrs"] == ["matt@firm.com.au"]
    assert query.pick_sender("Nobody Here", rows) is None


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def execute(self, sql, binds=None):
        self.executed.append((sql, binds))

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class FakeConn:
    def __init__(self, rows=()):
        self.cur = FakeCursor(list(rows))

    def cursor(self):
        return self.cur


def test_resolve_sender_uses_named_binds():
    conn = FakeConn([("JB Hi-Fi", "offers@email.jbhifi.com.au", 4)])
    m = query.resolve_sender(conn, "JB Hi-Fi")
    sql, binds = conn.cur.executed[0]
    assert isinstance(binds, dict) and binds == {"t0": "%jb%", "t1": "%hifi%"} and ":t0" in sql
    assert '"SPAM"' in sql and m["addrs"] == ["offers@email.jbhifi.com.au"]


def test_filters_sender_addrs_and_local_dates():
    binds = {}
    sql = Filters(sender_addrs=["A@x.com", "b@y.com"], after=date(2026, 10, 1), before=date(2026, 10, 8),
                  tz="Australia/Sydney").sql(binds)
    assert "LOWER(i.sender_addr) IN (:f_sa0, :f_sa1)" in sql and binds["f_sa0"] == "a@x.com"
    # Sydney midnight 1 Oct (AEST, +10) is 14:00 UTC on 30 Sep; 8 Oct is after DST starts (+11)
    assert binds["f_after"] == datetime(2026, 9, 30, 14, 0) and binds["f_before"] == datetime(2026, 10, 7, 13, 0)
    binds = {}
    Filters(after=date(2026, 10, 1)).sql(binds)
    assert binds["f_after"] == datetime(2026, 10, 1)   # default unchanged: UTC midnight


# ---------- run ----------

def _hit(i, when, subj):
    return Hit(item_id=i, score=0.0, received_at=when, sender="Matt <m@x.com>", subject=subj, snippet="snip",
               thread_id=None)


def test_run_list_mode(monkeypatch):
    seen = {}

    def fake_search(conn, q, f, limit=10, newest=False):
        seen.update(q=q, f=f, limit=limit, newest=newest)
        return [_hit(1, "2026-10-02 22:30:00+00:00", "Invoice"), _hit(2, "2026-09-30 01:00:00+00:00", "Hello")]

    monkeypatch.setattr(query, "search", fake_search)
    monkeypatch.setattr(query, "resolve_sender",
                        lambda conn, s: {"phrase": s, "label": "Sam Taylor", "addrs": ["m@x.com"]})
    conn = FakeConn([(1, "Asks you to pay invoice 42")])
    res = query.run(conn, "last 5 emails from Sam Taylor", FakeRouter(fail=True), today=TODAY,
                    tz="Australia/Sydney")
    assert res["mode"] == "list" and seen["newest"] and seen["limit"] == 5 and seen["q"] == ""
    assert seen["f"].sender_addrs == ["m@x.com"] and seen["f"].tz == "Australia/Sydney"
    assert res["items"][0]["summary"] == "Asks you to pay invoice 42" and res["items"][1]["summary"] == "snip"
    assert res["items"][0]["date"] == "2026-10-03"   # 22:30 UTC on 2 Oct is 3 Oct in Sydney
    assert "from Sam Taylor (m@x.com)" in res["interpreted"] and "newest" in res["interpreted"]
    sql, binds = conn.cur.executed[0]
    assert "FROM decisions" in sql and binds == {"i0": 1, "i1": 2}


def test_run_answer_mode(monkeypatch):
    seen = {}

    def fake_ask(conn, question, router, filters=None, k=8, per_source_chars=1500, retrieval_query=None,
                 newest=False):
        seen.update(question=question, filters=filters, k=k, rq=retrieval_query, newest=newest)
        return {"answer": "20% off headphones [1]", "model": "ollama:gemma4",
                "sources": [{"n": 1, "item_id": 7, "date": "2026-10-01", "from": "JB", "subject": "Perks"}]}

    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    monkeypatch.setattr(query, "resolve_sender",
                        lambda conn, s: {"phrase": s, "label": "JB Hi-Fi", "addrs": ["offers@email.jbhifi.com.au"]})
    r = FakeRouter(json.dumps({"mode": "answer", "topic": "perks offers deals", "sender": "JB Hi-Fi", "after": "",
                               "before": "", "limit": 8, "sort": "newest"}))
    res = query.run(None, "What are the latest perks from JB Hi-Fi?", r, today=TODAY, tz="Australia/Sydney")
    assert res["mode"] == "answer" and res["answer"].startswith("20% off")
    assert seen["question"] == "What are the latest perks from JB Hi-Fi?" and seen["rq"] == "perks offers deals"
    assert seen["newest"] and seen["filters"].sender_addrs == ["offers@email.jbhifi.com.au"]
    assert res["query"]["sender_match"]["label"] == "JB Hi-Fi"


def test_run_answer_unresolved_role_becomes_topic(monkeypatch):
    seen = {}
    monkeypatch.setattr(ask_mod, "ask", lambda conn, q, router, f=None, k=8, retrieval_query=None, newest=False:
                        seen.update(rq=retrieval_query, f=f) or {"answer": "x", "sources": []})
    monkeypatch.setattr(query, "resolve_sender", lambda conn, s: None)
    r = FakeRouter(json.dumps({"mode": "answer", "topic": "BAS", "sender": "accountant", "after": "2026-08-01",
                               "before": "2026-09-01", "limit": 8, "sort": "relevant"}))
    res = query.run(None, "what did the accountant say about BAS in August", r, today=TODAY, tz="Australia/Sydney")
    assert seen["rq"] == "accountant BAS" and seen["f"].sender is None and seen["f"].sender_addrs is None
    assert seen["f"].after == date(2026, 8, 1) and "no exact sender match" in res["interpreted"]


# ---------- Telegram / web rendering ----------

def _list_res(n):
    return {"mode": "list", "interpreted": "list · from <Matt> · newest · 5",
            "query": {"sort": "newest", "topic": "", "sender": "Matt", "sender_match": {"label": "Matt <M>",
                                                                                       "addrs": ["m@x"]}},
            "items": [{"item_id": i, "date": "2026-10-03", "sender": "Matt", "subject": f"<b>Hi {i}</b>",
                       "summary": "a & b"} for i in range(n)]}


def test_telegram_list_rendering_escapes_and_caps_buttons():
    text, kb = telegram.render_query_result(_list_res(8), lambda i: f"https://e.x/item/{i}")
    assert "📬 <b>Last 8 from Matt &lt;M&gt;</b>" in text
    assert "• 3 Oct · &lt;b&gt;Hi 0&lt;/b&gt; — a &amp; b" in text and "<b>Hi" not in text
    assert "Interpreted as: list · from &lt;Matt&gt;" in text
    assert len(kb["inline_keyboard"]) == 5 and kb["inline_keyboard"][0][0]["url"] == "https://e.x/item/0"
    _, kb = telegram.render_query_result(_list_res(2), lambda i: None)
    assert kb is None
    text, kb = telegram.render_query_result({**_list_res(0)}, None)
    assert "Nothing matched" in text and kb is None


def test_telegram_answer_rendering_and_free_text(monkeypatch):
    res = {"mode": "answer", "answer": "Use <code> [1]", "interpreted": "answer · newest · 8",
           "sources": [{"n": 1, "item_id": 3, "date": "2026-10-01 00:00", "from": "A <a@x>", "subject": "S"}],
           "query": {}}
    text, kb = telegram.render_query_result(res)
    assert "Use &lt;code&gt; [1]" in text and "<i>Interpreted as: answer · newest · 8</i>" in text and kb is None

    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    monkeypatch.setattr(query, "run", lambda conn, q, router: _list_res(3))
    from test_brief_telegram import FakeAPI
    bot = telegram.Bot(FakeAPI())
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "last 3 emails from Matt"}})
    assert "Last 3 from Matt" in bot.api.sent[-1][1]


def test_web_ask_fragment_renders_list_and_answer():
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    t = env.get_template("_ask.html")
    out = t.render(res=_list_res(2), head="Last 2 from Matt", error=None)
    assert 'href="/item/1"' in out and "&lt;b&gt;Hi 1&lt;/b&gt;" in out and "built-in method" not in out
    out = t.render(res={"mode": "answer", "answer": "Yes [1]", "interpreted": "answer",
                        "sources": [{"n": 1, "item_id": 9, "date": "2026-10-01", "from": "A <a@x>", "subject": "S"}]},
                   head="", error=None)
    assert 'href="/item/9"' in out and "A &lt;a@x&gt;" in out
    out = env.get_template("index.html").render(
        user="u", page="home", waiting=0, status={"accounts": [], "embedding_backlog": 0},
        tstats={"decisions": 0, "waiting_review": 0, "agreement": None}, needs={"alerts": [], "awaiting_reply": []})
    assert 'hx-post="/ask"' in out


ORG_ROWS = [("NSFC General Committee", "general.committee@nsfc.example.org", 12),
            ("NSFC Treasurer", "treasurer@nsfc.example.org", 7),
            ("NSFC Registrar", "registrar@mail.nsfc.example.org", 3),
            ("Jo Bloggs", "jo@gmail.com", 4)]


def test_group_word_means_whole_organisation():
    m = query.pick_org("NSFC Committee", ORG_ROWS)
    assert m["domains"] == ["nsfc.example.org"]                      # subdomain folded into the club's domain
    assert "treasurer@nsfc.example.org" in m["addrs"] and "jo@gmail.com" not in m["addrs"]
    q = query.Query(mode="list", topic="", sender="NSFC Committee", after=None, before=None, limit=5,
                    sort="newest", account=None, original="messages from the NSFC committee")
    assert "from NSFC Committee (anyone @nsfc.example.org)" in query.describe(q, m)


def test_org_scope_never_free_mail_and_bare_name_needs_exact_label():
    assert query.pick_org("Gmail team", [("Jo", "jo@gmail.com", 3)]) is None
    assert query.pick_org("Matt", [("Shop", "offers@mattressworld.example.com", 9)]) is None   # not a person -> org
    assert query.pick_org("NSFC", ORG_ROWS)["domains"] == ["nsfc.example.org"]


def test_domain_filter_sql_uses_named_binds():
    binds: dict = {}
    sql = Filters(sender_domains=["nsfc.example.org"]).sql(binds)
    assert ":f_sd0" in sql and ":f_ss0" in sql
    assert binds["f_sd0"] == "%@nsfc.example.org" and binds["f_ss0"] == "%.nsfc.example.org"


def test_specific_role_still_resolves_to_one_mailbox():
    m = query.pick_sender("NSFC treasurer", ORG_ROWS)
    assert m["addrs"] == ["treasurer@nsfc.example.org"]


def test_org_scope_ignores_articles():
    assert query.pick_org("the NSFC committee", ORG_ROWS)["domains"] == ["nsfc.example.org"]
    q = query.quick_parse("messages from the NSFC Committee this week", date(2026, 10, 7))
    assert q.sender and query.pick_org(q.sender, ORG_ROWS)["domains"] == ["nsfc.example.org"]
