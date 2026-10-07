from email.message import EmailMessage

from emaild import brief, triage
from emaild.normalise import parse_auth_results, parse_mime
from emaild.search import Filters


def _item(**kw):
    base = dict(id=1, account_id=1, account="me@example.com", sender_addr="andrew.m.photos@freemail.example",
                sender_name="Chris Morgan", recipients={"to": [{"addr": "me@example.com"}], "cc": []},
                subject="Photos from the weekend", received_at="2026-10-06", body="Click here to view the photos",
                labels=["INBOX"], meta={}, attachments=[], is_from_me=False, rfc_message_id="<x@y>")
    base.update(kw)
    return base


def test_auth_results_parsed_from_topmost_header_only():
    assert parse_auth_results(["mx.google.com; dkim=pass header.i=@x.com; spf=pass smtp.mailfrom=x.com; "
                               "dmarc=pass (p=REJECT) header.from=x.com", "evil; dmarc=fail"]) == \
        {"dkim": "pass", "spf": "pass", "dmarc": "pass"}
    m = EmailMessage()
    m["From"] = "a@b.c"
    m["Authentication-Results"] = "mx.google.com; spf=softfail smtp.mailfrom=b.c; dmarc=fail header.from=b.c"
    m.set_content("hi")
    assert parse_mime(m.as_bytes()).meta["auth"] == {"spf": "softfail", "dmarc": "fail"}


def test_gmail_spam_label_wins():
    p = triage.security_check(None, _item(labels=["SPAM"]), {})
    assert p.category == "spam" and p.action == "archive" and not triage.needs_review(p, 0.75)


def test_failed_authentication_is_held_back_without_review():
    p = triage.security_check(None, _item(meta={"auth": {"spf": "fail", "dkim": "none", "dmarc": "fail"}}), {})
    assert p.category == "suspicious" and "phishing" in p.reasons and not triage.needs_review(p, 0.75)
    assert triage.security_check(None, _item(meta={"auth": {"spf": "pass", "dkim": "pass", "dmarc": "pass"}}), {}) is None


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, binds=None):
        self.binds, self.sql = binds, sql

    def __iter__(self):
        return iter(self.rows if "sender_name" in self.sql else [])

    def fetchone(self):
        return (0,)


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur


def test_display_name_impersonation_of_known_contact():
    conn = _Conn([("andrew@realfirm.com.au",)])
    p = triage.security_check(conn, _item(), {"replied": 0, "sent_to": 0})
    assert p.category == "suspicious" and "andrew@realfirm.com.au" in p.reasons
    # the real address itself is fine
    assert triage.security_check(_Conn([("andrew@realfirm.com.au",)]),
                                 _item(sender_addr="andrew@realfirm.com.au"), {"replied": 3}) is None
    # single-word names are too generic to judge
    assert triage.security_check(_Conn([("support@real.com",)]), _item(sender_name="Support"), {}) is None


def test_unsafe_mail_excluded_from_search_and_brief_by_default():
    binds = {}
    assert '"SPAM"' in Filters().sql(binds) and "suspicious" in Filters().sql(binds)
    assert "SPAM" not in Filters(include_spam=True).sql(binds)
    b = {"period": {"since": "x", "since_local": "Wed 08:00"}, "received": 5, "by_action": {}, "alerts": [],
         "awaiting_reply": [], "important": [], "new_senders": [], "waiting_review": 0, "account_problems": [],
         "held_back": 3}
    assert "3 spam/suspicious held back" in brief.render_telegram(b)
