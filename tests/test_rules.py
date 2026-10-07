import json
from contextlib import contextmanager
from datetime import date

import pytest
from jinja2 import Environment, FileSystemLoader

from emaild import db, query, rules, telegram, triage
from emaild.llm.base import LLMResult
from emaild.triage import Proposal

TODAY = date(2026, 10, 7)
N = {"then_importance": "none", "then_category": "none"}


class FakeRouter:
    def __init__(self, text=None, fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def chat(self, task, messages, *, policy="local_only", schema=None, conn=None, temperature=0.1):
        self.calls.append(dict(task=task, messages=messages, schema=schema, temperature=temperature, policy=policy))
        if self.fail:
            raise RuntimeError("ollama down")
        t = self.text(messages) if callable(self.text) else self.text
        return LLMResult(t, "gemma4", "ollama", True)


def llm(**kw):
    base = {"kind": "rule", "name": "", "senders": [], "subject_words": [], "topic": "", "then_action": "none",
            "then_importance": "none", "then_category": "none", "else_action": "none", "floor": "none"}
    base.update(kw)
    return json.dumps(base)


KNOWN = {"rugby australia": ["news@rugby.example.org"],
         "australian grand prix": ["info@grandprix.example.org", "tickets@grandprix.example.org"],
         "riverside rovers": ["secretary@riversiderovers.example.org"],
         "strava": ["no-reply@strava.com"]}


@pytest.fixture
def resolver(monkeypatch):
    def fake(conn, phrase):
        addrs = KNOWN.get(phrase.lower())
        return {"phrase": phrase, "label": phrase, "addrs": addrs} if addrs else None
    monkeypatch.setattr(query, "resolve_sender", fake)


# ---------- compile: the five motivating cases ----------

def test_compile_tickets_rule(resolver):
    r = FakeRouter(llm(name="Ticket sales", senders=["Rugby Australia", "the Australian Grand Prix"],
                       topic="tickets or a ballot going on sale", then_action="alert", else_action="archive"))
    c = rules.compile_rule("From Rugby Australia or the Australian Grand Prix, alert me when tickets or a ballot "
                           "go on sale; archive the rest.", r, conn=object(), today=TODAY)
    comp = c["compiled"]
    assert c["kind"] == "rule" and c["source"] == "llm" and c["name"] == "Ticket sales"
    assert comp["match"]["senders"] == ["Rugby Australia", "Australian Grand Prix"]
    assert comp["match"]["sender_addrs"] == ["news@rugby.example.org", "info@grandprix.example.org", "tickets@grandprix.example.org"]
    assert comp["condition"]["topic"] == "tickets or a ballot going on sale" and comp["read_with_model"] is True
    assert comp["then"]["action"] == "alert" and comp["else"]["action"] == "archive"
    rb = c["readback"]
    assert rb.startswith("From Rugby Australia (news@rugby.example.org) or Australian Grand Prix (info@grandprix.example.org "
                         "+1 more): if it's about tickets or a ballot going on sale → alert (Needs attention); "
                         "otherwise → archive.")
    assert rb.endswith("Gemma will read every email from these senders.")
    call = r.calls[0]
    assert call["schema"] is rules.LLM_SCHEMA and call["temperature"] == 0 and call["policy"] == "local_only"
    assert "2026-10-07" in call["messages"][0]["content"]
    assert call["messages"][1]["content"].startswith("From Rugby Australia")   # only the user's words


def test_compile_canteen_rule_infers_club_domain(resolver):
    r = FakeRouter(llm(name="Canteen roster", senders=["Riverside Rovers"], topic="the canteen roster",
                       then_action="alert"))
    c = rules.compile_rule("Anything from Riverside Rovers about the canteen roster goes to Needs attention", r,
                           conn=object(), today=TODAY)
    m = c["compiled"]["match"]
    assert m["domains"] == ["riversiderovers.example.org"] and m["sender_addrs"] == ["secretary@riversiderovers.example.org"]
    assert c["compiled"]["else"] is None
    assert "anyone @riversiderovers.example.org" in c["readback"]
    assert "otherwise emAIl decides as usual" in c["readback"]


def test_compile_strava_and_accountant_and_guidance(resolver):
    c = rules.compile_rule("Always archive Strava emails", FakeRouter(llm(name="Archive Strava", senders=["Strava"],
                                                                          then_action="archive")), object(), TODAY)
    assert c["compiled"]["read_with_model"] is False and c["compiled"]["then"]["action"] == "archive"
    assert c["readback"] == "From Strava (no-reply@strava.com) or anyone @strava.com → archive."
    c = rules.compile_rule("Never archive anything from my accountant",
                           FakeRouter(llm(name="Accountant", senders=["my accountant"], floor="keep")), object(), TODAY)
    assert c["compiled"]["floor"] == "keep" and c["compiled"]["match"]["senders"] == ["accountant"]
    assert "never archived" in c["readback"] and "by name" in c["readback"]
    assert any("couldn't find any mail" in w for w in c["warnings"])
    c = rules.compile_rule("I care less about conference marketing unless I'm speaking",
                           FakeRouter(llm(kind="guidance", name="Conference marketing")), object(), TODAY)
    assert c["kind"] == "guidance" and c["compiled"] == {}
    assert c["readback"].startswith("Guidance (no fixed action): “I care less about conference marketing")


def test_guidance_prefix_needs_no_model():
    r = FakeRouter(fail=True)
    c = rules.compile_rule("guidance: newsletters from cycling clubs matter to me", r, None, TODAY)
    assert c["kind"] == "guidance" and c["original_text"] == "newsletters from cycling clubs matter to me"
    assert r.calls == []


# ---------- fallback parser + validation ----------

@pytest.mark.parametrize("text,senders,action,floor,topic", [
    ("Always archive Strava emails", ["Strava"], "archive", None, None),
    ("never archive anything from my accountant", ["accountant"], None, "keep", None),
    ("Alert me about anything from Ticketek or Moshtix", ["Ticketek", "Moshtix"], "alert", None, None),
    ("Keep everything from the school", ["school"], "keep", None, None),
    ("Anything from Acme Events about logistics is urgent", ["Acme Events"], "alert", None, "logistics"),
    ("anything from Riverside Rovers about the canteen roster goes to needs attention", ["Riverside Rovers"], "alert",
     None, "the canteen roster"),
])
def test_fallback_parser(text, senders, action, floor, topic):
    c = rules.compile_rule(text, FakeRouter(fail=True), None, TODAY)
    assert c["source"] == "pattern" and c["compiled"]["match"]["senders"] == senders
    assert c["compiled"]["then"]["action"] == action and c["compiled"]["floor"] == floor
    assert c["compiled"]["condition"]["topic"] == topic
    assert c["warnings"] and "without the model" in c["warnings"][0]


def test_unparseable_asks_to_rephrase():
    c = rules.compile_rule("hmm, do the thing with the stuff", FakeRouter(fail=True), None, TODAY)
    assert "error" in c and "guidance:" in c["error"]
    assert "error" in rules.compile_rule("   ", None)


def test_validate_rejects_bad_compiled():
    ok = {"match": {"senders": ["Strava"]}, "then": {"action": "archive"}}
    assert rules.validate_compiled(ok)["then"]["action"] == "archive"
    for bad, msg in [({"match": {"senders": ["x"]}, "then": {"action": "delete"}}, "then.action"),
                     ({"match": {}, "then": {"action": "keep"}}, "doesn't say which"),
                     ({"match": {"senders": ["x"]}, "then": {}}, "doesn't say what"),
                     ({"match": {"senders": ["x"]}, "then": {"action": "keep"}, "else": {"action": "archive"}},
                      "otherwise"),
                     ({"match": {"sender_addrs": ["not-an-address"]}, "then": {"action": "keep"}}, "address"),
                     ({"match": {"senders": ["x"]}, "then": {"category": "spam"}}, "category"),
                     ({"match": {"senders": ["x"]}, "floor": "archive"}, "floor")]:
        with pytest.raises(ValueError, match=msg):
            rules.validate_compiled(bad)


def test_model_junk_falls_back_to_patterns():
    for r in (FakeRouter(llm(senders=["Strava"], then_action="delete")),   # unknown action
              FakeRouter(llm(senders=[], then_action="archive")),          # nothing to match
              FakeRouter("sorry"), FakeRouter(fail=True)):
        c = rules.compile_rule("Always archive Strava emails", r, None, TODAY)
        assert c["source"] == "pattern" and c["compiled"]["then"]["action"] == "archive"


# ---------- matcher (pure) ----------

def test_matcher_normalises_like_query():
    c = rules.validate_compiled({"match": {"senders": ["JB Hi-Fi"]}, "then": {"action": "keep"}})
    assert rules.match_how(c, {"sender_addr": "offers@email.jbhifi.com.au", "sender_name": ""}) == "name"
    assert rules.match_how(c, {"sender_addr": "x@y.com", "sender_name": "JB HiFi Perks"}) == "name"
    assert not rules.matches(c, {"sender_addr": "x@y.com", "sender_name": "Harvey Norman"})


def test_matcher_addresses_domains_subject_account():
    c = rules.validate_compiled({"match": {"senders": ["riversiderovers.example.org"], "domains": ["riversiderovers.example.org"],
                                           "subject_any": ["roster"]}, "then": {"action": "alert"}})
    item = {"sender_addr": "sec@mail.riversiderovers.example.org", "sender_name": "", "subject": "Canteen Rosters for May"}
    assert rules.match_how(c, item) == "domain"
    assert not rules.matches(c, {**item, "subject": "AGM notice"})
    assert not rules.matches(c, {**item, "sender_addr": "x@riversiderovers.example.org.evil.com"})   # look-alike
    assert not rules.matches(c, {**item, "sender_addr": "x@evil.com", "sender_name": "riversiderovers.example.org"})
    c2 = rules.validate_compiled({"match": {"sender_addrs": ["A@B.com"], "account": "Work@X.com"},
                                  "then": {"action": "keep"}})
    assert rules.match_how(c2, {"sender_addr": "a@b.com", "account": "work@x.com"}) == "addr"
    assert not rules.matches(c2, {"sender_addr": "a@b.com", "account": "home@x.com"})


def _rule(id, compiled, name=None, priority=100, kind="rule", text=""):
    return {"id": id, "name": name or f"r{id}", "kind": kind, "priority": priority, "original_text": text,
            "compiled": compiled, "readback": f"readback {id}"}


STRAVA = {"match": {"senders": ["Strava"]}, "then": {"action": "archive"}}
TICKETS = {"match": {"senders": ["Rugby Australia"], "sender_addrs": ["news@rugby.example.org"]},
           "condition": {"topic": "tickets or a ballot going on sale"},
           "then": {"action": "alert"}, "else": {"action": "archive"}}
ACCOUNTANT = {"match": {"senders": ["Smith Accounting"]}, "floor": "keep"}


def test_evaluate_priority_overrides_and_floors():
    item = {"sender_addr": "no-reply@strava.com", "sender_name": "Strava"}
    imp = {"match": {"senders": ["Strava"]}, "then": {"importance": "low"}}
    floor = {"match": {"senders": ["Strava"]}, "floor": "keep"}
    keep = {"match": {"senders": ["Strava"]}, "then": {"action": "keep"}}
    rm = rules.evaluate([_rule(3, STRAVA), _rule(1, imp), _rule(2, floor), _rule(4, keep, priority=50)], item)
    assert rm.decider["id"] == 4 and not rm.conditional and rm.explicit_sender is False
    assert [r["id"] for r in rm.matched] == [4, 1, 2, 3] and rm.overrides == {"importance": "low"}
    assert [r["id"] for r in rm.floors] == [2] and rm.bypass_heuristic
    assert not rules.evaluate([_rule(1, STRAVA)], {"sender_addr": "a@b.com", "sender_name": "Bob"}).matched
    assert rules.evaluate([_rule(9, {"match": {}, "then": {"action": "x"}})], item).decider is None   # corrupt


def test_guidance_text_is_capped():
    gs = [_rule(i, {}, kind="guidance", text="I care less about conference marketing " * 3) for i in range(100)]
    txt = rules.guidance_text(gs + [_rule(500, STRAVA)])
    assert txt.startswith("- I care less") and len(txt) <= rules.GUIDANCE_CHARS


# ---------- intent parsing ----------

def test_parse_until_and_intents():
    assert rules.parse_until("February", TODAY) == date(2027, 2, 1)
    assert rules.parse_until("december", TODAY) == date(2026, 12, 1)
    assert rules.parse_until("2026-11-01", TODAY) == date(2026, 11, 1)
    assert rules.parse_until("15 March", TODAY) == date(2027, 3, 15)
    assert rules.parse_until("2 weeks", TODAY) == date(2026, 10, 21)
    assert rules.parse_until("the heat death", TODAY) is None
    assert rules.parse_intent("turn off the rugby rule until February", TODAY) == \
        {"op": "off", "ref": "rugby", "until": date(2027, 2, 1)}
    assert rules.parse_intent("pause rule 3", TODAY) == {"op": "off", "ref": "3", "until": None}
    assert rules.parse_intent("delete the strava rule", TODAY) == {"op": "rm", "ref": "strava"}
    assert rules.parse_intent("turn on the rugby rule", TODAY) == {"op": "on", "ref": "rugby"}
    assert rules.parse_intent("show my rules", TODAY) == {"op": "list"}
    assert rules.parse_intent("Always archive Strava emails", TODAY) == {"op": "add",
                                                                          "text": "Always archive Strava emails"}
    assert rules.parse_intent("rule: archive kudos from Strava", TODAY)["text"] == "archive kudos from Strava"
    for q in ("last 5 emails from Matt", "what did the accountant say about the BAS?", "anything from Matt today?",
              "never mind"):
        assert rules.parse_intent(q, TODAY) is None
    assert "error" in rules.parse_intent("turn off the rugby rule until whenever", TODAY)


# ---------- triage integration ----------

def _item(**kw):
    base = dict(id=1, account_id=1, account="me@example.com", sender_addr="news@rugby.example.org",
                sender_name="Rugby Australia", recipients={"to": [], "cc": []}, subject="Wallabies v All Blacks",
                received_at="2026-10-06 09:00:00", body="Big match news.", labels=["INBOX", "CATEGORY_PROMOTIONS"],
                meta={"list_unsubscribe": "<https://x>"}, attachments=[], is_from_me=False, rfc_message_id="")
    base.update(kw)
    return base


def model_says(action="keep", met=None, category="newsletter", confidence=0.9):
    d = {"summary": "s", "category": category, "importance": "normal", "needs_reply": False, "action": action,
         "confidence": confidence, "reasons": "model reasons"}
    if met is not None:
        d["rule_condition_met"] = met
    return json.dumps(d)


@pytest.fixture
def offline(monkeypatch):
    """No DB: sender stats empty, no examples, no overrides; security/duplicate checks off unless a test sets them."""
    monkeypatch.setattr(triage.senders, "get", lambda conn, addr: {})
    monkeypatch.setattr(triage, "security_check", lambda conn, item, st: None)
    monkeypatch.setattr(triage, "duplicate_of", lambda conn, item: None)
    monkeypatch.setattr(triage, "find_examples", lambda conn, item, exclude_item=None: [])
    monkeypatch.setattr(triage, "sender_overridden", lambda conn, s: False)
    monkeypatch.setattr(triage, "sender_keeps", lambda conn, s: False)


def test_no_rules_unchanged(offline):
    r = FakeRouter(model_says("keep"))
    p = triage.decide(None, r, _item(), "Stu", [])
    assert p.source == "heuristic" and r.calls == []          # bulk mail settled by the heuristic as before
    p = triage.decide(None, r, _item(meta={}, labels=["INBOX"]), "Stu", [])
    assert p.source == "llm" and r.calls[0]["schema"] is triage.SCHEMA and "rule_condition_met" not in \
        r.calls[0]["messages"][0]["content"]


def test_security_wins_over_rules(offline, monkeypatch):
    sec = Proposal("s", "suspicious", "low", False, "archive", 0.97, "phish", source="security")
    monkeypatch.setattr(triage, "security_check", lambda conn, item, st: sec)
    r = FakeRouter(model_says("keep"))
    for rs in ([_rule(1, TICKETS)], [_rule(2, {"match": {"senders": ["Rugby Australia"]}, "then": {"action": "alert"}})],
               [_rule(3, {"match": {"senders": ["Rugby Australia"]}, "floor": "keep"})]):
        p = triage.decide(None, r, _item(), "Stu", rs)
        assert p is sec and p.action == "archive" and not p.rule_ids
    assert r.calls == []


def test_model_phishing_verdict_beats_rule(offline):
    r = FakeRouter(model_says("keep", met=True, category="suspicious"))
    p = triage.decide(None, r, _item(), "Stu", [_rule(1, TICKETS), _rule(2, {"match": {"senders": ["Rugby"]},
                                                                             "floor": "keep"})])
    assert p.source == "security" and p.action == "archive" and not p.rule_ids


def test_deterministic_rule_skips_model_and_heuristic(offline):
    r = FakeRouter(model_says("keep"))
    item = _item(sender_addr="no-reply@strava.com", sender_name="Strava", meta={}, labels=["INBOX"])
    p = triage.decide(None, r, item, "Stu", [_rule(7, STRAVA, name="Archive Strava")])
    assert r.calls == [] and p.source == "rule" and p.action == "archive" and p.importance == "low"
    assert p.rule_ids == [7] and p.reasons.startswith("Rule: Archive Strava") and p.confidence == 0.95
    assert not triage.needs_review(p, 0.75)
    alert = triage.decide(None, r, item, "Stu", [_rule(8, {"match": {"senders": ["Strava"]},
                                                           "then": {"action": "alert"}})])
    assert alert.action == "alert" and alert.importance == "high" and not triage.needs_review(alert, 0.75)


def test_rule_never_archives_personal_mail_matched_by_name(offline):
    friend = _item(sender_addr="jo@gmail.com", sender_name="Jo Strava", meta={}, labels=["INBOX"],
                   recipients={"to": [{"addr": "me@example.com"}], "cc": []})
    p = triage.decide(None, FakeRouter(), friend, "Stu", [_rule(7, STRAVA)])
    assert p.action == "keep" and p.guard == "personal" and triage.needs_review(p, 0.75)
    by_addr = {"match": {"sender_addrs": ["jo@gmail.com"]}, "then": {"action": "archive"}}
    assert triage.decide(None, FakeRouter(), friend, "Stu", [_rule(8, by_addr)]).action == "archive"


def test_topic_rule_bypasses_heuristic_and_applies_then_else(offline):
    rs = [_rule(5, TICKETS, name="Ticket sales")]
    r = FakeRouter(model_says("keep", met=True))
    p = triage.decide(None, r, _item(subject="Ballot opens Friday"), "Stu", rs)
    assert len(r.calls) == 1 and r.calls[0]["schema"] is triage.SCHEMA_RULE      # bulk mail, but the model read it
    sys_ = r.calls[0]["messages"][0]["content"]
    assert "Is this email about tickets or a ballot going on sale?" in sys_ and "rule_condition_met" in sys_
    assert "Ticket sales" in sys_ and "Ballot opens Friday" not in sys_            # email stays in the user message
    assert p.source == "rule+llm" and p.action == "alert" and p.importance == "high" and p.rule_ids == [5]
    assert p.reasons.startswith("Rule “Ticket sales”: about tickets") and "model reasons" in p.reasons
    assert not triage.needs_review(p, 0.75)

    p = triage.decide(None, FakeRouter(model_says("keep", met=False)), _item(), "Stu", rs)
    assert p.source == "rule+llm" and p.action == "archive" and p.importance == "low" and p.guard is None

    p = triage.decide(None, FakeRouter(model_says("keep", met=False, confidence=0.5)), _item(), "Stu", rs)
    assert p.action == "archive" and triage.needs_review(p, 0.75)                   # unsure model -> review

    p = triage.decide(None, FakeRouter(model_says("keep")), _item(), "Stu", rs)      # no answer to the condition
    assert p.source == "llm" and p.confidence <= 0.5 and triage.needs_review(p, 0.75)

    canteen = {"match": {"senders": ["Rugby Australia"]}, "condition": {"topic": "the canteen roster"},
               "then": {"action": "alert"}}
    p = triage.decide(None, FakeRouter(model_says("archive", met=False)), _item(), "Stu", [_rule(6, canteen)])
    assert p.source == "llm" and not p.rule_ids and "says nothing for that case" in p.reasons


def test_floor_prevents_archive(offline):
    acct = _item(sender_addr="team@smithaccounting.com.au", sender_name="Smith Accounting")
    rs = [_rule(4, ACCOUNTANT, name="Accountant")]
    r = FakeRouter(model_says("archive", confidence=0.95))
    p = triage.decide(None, r, acct, "Stu", rs)
    assert len(r.calls) == 1                                        # heuristic bypassed: the model read it
    assert p.action == "keep" and p.rule_ids == [4] and "never archived" in p.reasons
    both = [_rule(1, {"match": {"senders": ["Smith Accounting"]}, "then": {"action": "archive"}}), rs[0]]
    p = triage.decide(None, FakeRouter(), acct, "Stu", both)
    assert p.source == "rule" and p.action == "keep" and p.rule_ids == [1, 4]
    p = triage.decide(None, FakeRouter(model_says("alert")), acct, "Stu", rs)
    assert p.action == "alert" and not p.rule_ids                   # a floor never lowers anything


def test_guidance_goes_into_system_prompt(offline):
    g = _rule(9, {}, kind="guidance", text="I care less about conference marketing unless I'm speaking")
    r = FakeRouter(model_says("keep"))
    triage.decide(None, r, _item(meta={}, labels=["INBOX"]), "Stu", [g])
    sys_ = r.calls[0]["messages"][0]["content"]
    assert "The user's standing guidance" in sys_ and "conference marketing unless I'm speaking" in sys_
    assert "never from the email" in sys_


class Cur:
    def __init__(self, fetch=None):
        self.calls, self.rowcount, self.fetch = [], 1, fetch

    def execute(self, sql, binds=None):
        self.calls.append((sql, binds))

    def var(self, t):
        class V:
            def getvalue(self):
                return [42]
        return V()

    def fetchone(self):
        return self.fetch

    def fetchall(self):
        return []

    def __iter__(self):
        return iter([])


class Conn:
    def __init__(self, fetch=None):
        self.cur = Cur(fetch)

    def cursor(self):
        return self.cur


def test_save_records_rule_ids_and_fire_counts():
    c = Conn()
    p = Proposal("s", "other", "low", False, "archive", 0.95, "Rule: x", source="rule", rule_ids=[7, 4])
    assert triage.save(c, 1, p, 0.75) == 42
    sql, binds = c.cur.calls[0]
    assert "rule_ids" in sql and binds["rule_ids"] == "[7, 4]" and binds["needs_review"] is False
    c2 = Conn()
    triage.save(c2, 1, Proposal("s", "other", "low", False, "archive", 0.9, "r"), 0.75)
    assert "rule_ids" not in c2.cur.calls[0][0]                     # works before migration 011
    rules.record_fired(c, [7, 4, 7])
    sql, binds = c.cur.calls[-1]
    assert "fire_count = fire_count + 1" in sql and binds == {"r0": 7, "r1": 4} and ":r0" in sql


def test_reconsider_open_decisions(offline):
    item = _item(sender_addr="no-reply@strava.com", sender_name="Strava")
    c = Conn()
    assert triage.reconsider(c, 11, "heuristic", "keep", item, {}, [_rule(7, STRAVA)]) == "rule"
    sql, binds = c.cur.calls[0]
    assert sql.lstrip().startswith("UPDATE decisions") and binds["a"] == "archive" and binds["rids"] == "[7]"
    assert "status = 'proposed'" in sql
    c = Conn()                                    # conditional rule now covers it: back to triage (model)
    rug = _item()
    assert triage.reconsider(c, 12, "heuristic", "archive", rug, {}, [_rule(5, TICKETS)]) == "retriage"
    assert c.cur.calls[0][0].startswith("DELETE FROM decisions") and "status = 'proposed'" in c.cur.calls[0][0]
    c = Conn()                                    # ...but never outside the triage window / once cleared
    assert triage.reconsider(c, 12, "heuristic", "archive", rug, {}, [_rule(5, TICKETS)], retriage_ok=False) is None
    assert c.cur.calls == []
    c = Conn(fetch=['[5]'])                       # already decided by this rule: left alone
    assert triage.reconsider(c, 12, "rule+llm", "alert", rug, {}, [_rule(5, TICKETS)]) is None
    c = Conn()                                    # its rule went away
    assert triage.reconsider(c, 13, "rule", "archive", item, {}, []) == "retriage"
    c = Conn()                                    # floor: archive -> keep in place
    acct = _item(sender_addr="a@smithaccounting.com.au", sender_name="Smith Accounting")
    assert triage.reconsider(c, 14, "heuristic", "archive", acct, {}, [_rule(4, ACCOUNTANT)]) == "floor"
    assert "action = 'keep'" in c.cur.calls[0][0] and c.cur.calls[0][1]["rids"] == "[4]"
    c = Conn()
    assert triage.reconsider(c, 15, "llm", "keep", acct, {}, [_rule(4, ACCOUNTANT)]) is None and not c.cur.calls


def test_create_stores_pending_with_version(monkeypatch, resolver):
    monkeypatch.setattr(rules.store, "audit", lambda *a, **k: None)
    c = Conn()
    r = rules.create(c, "Always archive Strava emails", FakeRouter(llm(name="Strava", senders=["Strava"],
                                                                         then_action="archive")), today=TODAY)
    assert r["id"] == 42 and r["status"] == "pending" and r["readback"].startswith("From Strava")
    ins, binds = c.cur.calls[0]
    assert "'pending'" in ins and json.loads(binds["comp"])["then"]["action"] == "archive"
    assert "rule_versions" in c.cur.calls[1][0] and c.cur.calls[1][1]["ver"] == 1
    assert all(isinstance(b, dict) for _, b in c.cur.calls)        # named binds only
    bad = rules.create(Conn(), "do the thing", FakeRouter(fail=True))
    assert "error" in bad


# ---------- surfaces ----------

class FakeAPI:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": 500 + len(self.sent)}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


PENDING = {"id": 31, "name": "Ticket <sales>", "kind": "rule", "status": "pending", "version": 1,
           "readback": "From Rugby Australia (news@rugby.example.org): if it's about tickets → alert", "warnings": [],
           "fire_count": 0, "original_text": "x"}


@pytest.fixture
def bot(monkeypatch):
    link = {"ctx": db.UserCtx(1, 1, "u@x"), "chat_id": 77, "detail": "summary", "muted": False, "linked_at": None}
    monkeypatch.setattr(telegram, "link_for_chat", lambda chat: link)

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    return telegram.Bot(FakeAPI())


def test_telegram_rule_save_flow(bot, monkeypatch):
    made = []
    monkeypatch.setattr(rules, "create", lambda conn, text, router, actor="user": made.append(text) or PENDING)
    confirmed = []
    monkeypatch.setattr(rules, "confirm", lambda conn, rid, actor="user": confirmed.append(rid) or
                        {"rule_id": rid, "active": True, "reapplied": {"updated": 2, "retriage": 1}})
    bot.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "/rule From Rugby Australia, alert "
                                                                                 "me when tickets go on sale"}})
    assert made == ["From Rugby Australia, alert me when tickets go on sale"]
    _, text, kb = bot.api.sent[-1]
    assert "Ticket &lt;sales&gt;" in text and "news@rugby.example.org" in text and "Save it?" in text
    datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert datas == ["r:y:31", "r:n:31"] and all(len(d.encode()) < 64 for d in datas)
    bot.handle_update({"update_id": 2, "callback_query": {"id": "q", "data": "r:y:31",
                                                          "message": {"chat": {"id": 77}, "message_id": 10}}})
    assert confirmed == [31]
    answered = [p for m, p in bot.api.calls if m == "answerCallbackQuery"][-1]
    assert "saved and on" in answered["text"] and "3 open decisions" in answered["text"]


def test_telegram_rule_cancel_list_and_free_text(bot, monkeypatch):
    deleted = []
    monkeypatch.setattr(rules, "get", lambda conn, rid: {**PENDING, "id": rid})
    monkeypatch.setattr(rules, "delete", lambda conn, rid, actor="user": deleted.append(rid) or True)
    bot.handle_update({"update_id": 3, "callback_query": {"id": "q", "data": "r:n:31",
                                                          "message": {"chat": {"id": 77}, "message_id": 10}}})
    assert deleted == [31]
    active = {**PENDING, "id": 4, "name": "Strava", "status": "active", "fire_count": 12}
    paused = {**PENDING, "id": 5, "name": "Rugby", "status": "paused", "paused_until": "2027-02-01 00:00"}
    monkeypatch.setattr(rules, "list_rules", lambda conn, include_deleted=False: [active, paused])
    bot.handle_update({"update_id": 4, "message": {"chat": {"id": 77}, "text": "/rules"}})
    _, text, kb = bot.api.sent[-1]
    assert "#4 Strava" in text and "fired 12×" in text and "until 2027-02-01" in text
    assert kb["inline_keyboard"][0][0]["callback_data"] == "r:p:4" and kb["inline_keyboard"][1][0]["callback_data"] \
        == "r:o:5"
    offs = []
    monkeypatch.setattr(rules, "find_rule", lambda conn, ref: paused if "rugby" in ref else None)
    monkeypatch.setattr(rules, "set_enabled", lambda conn, rid, en, until=None, actor="user":
                        offs.append((rid, en, until)) or True)
    bot.handle_update({"update_id": 5, "message": {"chat": {"id": 77}, "text": "turn off the rugby rule until "
                                                                                 "2027-02-01"}})
    assert offs == [(5, False, date(2027, 2, 1))] and "off until 2027-02-01" in bot.api.sent[-1][1]
    bot.handle_update({"update_id": 6, "message": {"chat": {"id": 77}, "text": "/rule rm nothing"}})
    assert "No rule matches" in bot.api.sent[-1][1]
    assert "/rules" in telegram.HELP and "/rule off" in telegram.HELP


def test_cli_rule_add_yes(monkeypatch, capsys):
    from emaild import cli, users

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(users, "resolve", lambda *a: db.UserCtx(1, 1, "u@x"))
    monkeypatch.setattr(db, "user_session", fake_session)
    monkeypatch.setattr(rules, "create", lambda conn, text, router, actor="user": {**PENDING, "name": "Ticket sales"})
    confirmed = []
    monkeypatch.setattr(rules, "confirm", lambda conn, rid, actor="user": confirmed.append(rid) or
                        {"rule_id": rid, "active": True, "reapplied": {"checked": 9, "updated": 2, "retriage": 0}})
    cli.main(["rule", "add", "From Rugby Australia, alert me when tickets go on sale", "--yes"])
    out = capsys.readouterr().out
    assert confirmed == [31] and "Ticket sales" in out and "news@rugby.example.org" in out and "rule 31 is on" in out
    assert "2 updated" in out


def test_web_rules_page_and_add(monkeypatch):
    from fastapi.testclient import TestClient

    from emaild import config, users
    from emaild.web import app as appmod

    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    active = {**PENDING, "id": 4, "name": "Strava <x>", "status": "active", "fire_count": 3,
              "last_fired_at": "2026-10-07 01:00", "paused_until": None}
    out = env.get_template("rules.html").render(rules=[active, {**PENDING, "status": "paused",
                                                                "paused_until": "2027-02-01 00:00"}],
                                                page="rules", waiting=0)
    assert 'class="on">Rules' in out and "Strava &lt;x&gt;" in out and "fired 3×" in out
    assert 'hx-post="/rules/4/off"' in out and "off until 2027-02-01" in out and 'hx-post="/rules/31/on"' in out

    monkeypatch.setenv("EMAILD_WEB_PASSWORD", "")
    config.settings.cache_clear()
    monkeypatch.setattr(users, "resolve", lambda *a: db.UserCtx(1, 1, "u@x"))

    @contextmanager
    def fake_session(ctx):
        yield object()

    monkeypatch.setattr(db, "user_session", fake_session)
    monkeypatch.setattr(rules, "create", lambda conn, text, router, actor="user": {**PENDING, "warnings": ["w <b>"]})
    c = TestClient(appmod.app)
    r = c.post("/rules", data={"text": "From Rugby Australia, alert me when tickets go on sale"})
    assert r.status_code == 200 and "Ticket &lt;sales&gt;" in r.text and 'hx-post="/rules/31/confirm"' in r.text
    assert "w &lt;b&gt;" in r.text and 'hx-post="/rules/31/cancel"' in r.text
    monkeypatch.setattr(rules, "create", lambda conn, text, router, actor="user": {"error": "Try <again>"})
    assert "Try &lt;again&gt;" in c.post("/rules", data={"text": "x"}).text
    monkeypatch.setattr(rules, "confirm", lambda conn, rid, actor="user": {"active": True})
    monkeypatch.setattr(rules, "get", lambda conn, rid: {**active, "id": rid})
    r = c.post("/rules/31/confirm")
    assert 'id="r31"' in r.text and "Turn off" in r.text
    assert "Can" in c.post("/rules/31/off", data={"until": "whenever"}).text
