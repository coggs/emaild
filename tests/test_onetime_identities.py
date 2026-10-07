from datetime import datetime

from emaild import identities, onetime, triage


def test_detects_codes_and_login_links_with_expiry():
    assert onetime.looks_one_time("Your Anthropic verification code",
                                  "Your verification code is 482913. This code expires in 10 minutes.")
    assert onetime.looks_one_time("Secure link to log in to Claude.ai",
                                  "Click the button below to sign in. This link will expire in 1 hour.")
    assert onetime.looks_one_time("123456 is your sign-in code", "Enter this code to finish signing in.")
    assert onetime.expiry_minutes("This code expires in 10 minutes.", 15) == 10
    assert onetime.expiry_minutes("This link is valid for 1 hour", 15) == 60
    assert onetime.expiry_minutes("no expiry stated", 15) == 15
    assert onetime.extract_code("Your verification code is 482913.") == "482913"
    assert onetime.extract_code("Your code: 123-456") == "123-456"


def test_not_fooled_by_ordinary_mail():
    assert not onetime.looks_one_time("Club training this Saturday", "Please confirm your attendance by Friday.")
    assert not onetime.looks_one_time("Invoice #4821", "Your invoice code is attached.")
    item = dict(subject="Your exclusive discount code", body="Use code SAVE2025 at checkout. Offer valid for 3 days.",
                labels=["CATEGORY_PROMOTIONS"], meta={"list_unsubscribe": "<x>"}, sender_addr="deals@shop.example",
                sender_name="Shop", recipients={"to": []}, account="me@x", received_dt=datetime(2026, 10, 7))
    assert triage.one_time_check(item) is None           # promotional mail is never a one-time code


def test_one_time_proposal_has_expiry_and_skips_review():
    item = dict(subject="Your verification code", body="Your code is 482913. It expires in 10 minutes.",
                labels=["INBOX"], meta={}, sender_addr="no-reply@accounts.example", sender_name="Example",
                recipients={"to": [{"addr": "me@x"}]}, account="me@x", received_dt=datetime(2026, 10, 7, 9, 0))
    p = triage.one_time_check(item)
    assert p.category == "one_time" and p.expires_at == datetime(2026, 10, 7, 9, 10)
    assert not triage.needs_review(p, 0.75) and not triage.spot_check(p, 1.0)


PROTECTED = [{"kind": "person", "name": "Alex Rivera",
              "allowed": ["president@riversiderovers.example.org", "@riversiderovers.example.org"]},
             {"kind": "org", "name": "Riverside Rovers", "allowed": ["@riversiderovers.example.org"]}]


def test_protected_person_and_org_impersonation():
    assert identities.check("Alex Rivera", "president@riversiderovers.example.org", PROTECTED) is None
    assert identities.check("Alex Rivera", "treasurer@riversiderovers.example.org", PROTECTED) is None   # same domain
    w = identities.check("Alex Rivera", "alex.rivera.office@gmail.com", PROTECTED)
    assert w and "protected person" in w and "gmail.com" in w
    assert identities.check("Alex Rivera (President)", "x@outlook.com", PROTECTED)
    assert identities.check("Riverside Rovers FC Committee", "admin@randomhost.example", PROTECTED)
    assert identities.check("Matthew Smith", "m@gmail.com", PROTECTED) is None


def test_address_hidden_in_display_name():
    w = identities.check("president@riversiderovers.example.org", "someone@gmail.com", [])
    assert w and "actually came from someone@gmail.com" in w
    assert identities.check("someone@gmail.com", "someone@gmail.com", []) is None


def test_allowed_normalisation():
    assert identities.normalise_allowed(["RiversideRovers.example.org", "President@RiversideRovers.example.org"]) == \
        ["@riversiderovers.example.org", "president@riversiderovers.example.org"]
    assert identities.is_allowed("a@mail.riversiderovers.example.org", ["@riversiderovers.example.org"])
    assert not identities.is_allowed("a@riversiderovers.example.org.evil.com", ["@riversiderovers.example.org"])


def test_security_check_uses_protected_identities():
    class Cur:
        def execute(self, sql, binds=None):
            self.sql = sql
        def __iter__(self):
            if "protected_identities" in self.sql:
                return iter([(1, p["kind"], p["name"], p["allowed"], None) for p in PROTECTED])
            return iter([])
        def fetchone(self):
            return (0,)

    class Conn:
        def cursor(self):
            return Cur()

    item = dict(id=1, account="me@x", sender_addr="alex.rivera.office@gmail.com", sender_name="Alex Rivera",
                recipients={"to": [{"addr": "me@x"}]}, subject="Urgent request", body="Are you available?",
                labels=["INBOX"], meta={}, attachments=[])
    p = triage.security_check(Conn(), item, {})
    assert p.category == "suspicious" and "protected person" in p.reasons


ORGS = [{"kind": "org", "name": "Riverside Rovers", "allowed": ["@riversiderovers.example.org"]},
        {"kind": "org", "name": "NHFC", "allowed": ["@northhillsfc.example.com"]}]


def _mail(name, addr, subject="Quick favour", to="jordan@northhillsfc.example.com"):
    return {"sender_name": name, "sender_addr": addr, "subject": subject,
            "recipients": {"to": [{"addr": to}], "cc": []}}


def test_role_impersonation_tied_to_protected_club():
    # sent to a club address, claims a role, from outside the club domain
    w = identities.role_check(_mail("President", "pres.office@outlook.com"), ORGS, engaged=False)
    assert w and "NHFC" in w
    # club named in the display name
    assert identities.role_check(_mail("Riverside Rovers President", "x@weird.example", to="me@gmail.com"),
                                 ORGS, engaged=True)
    # the real president, from the club domain
    assert identities.role_check(_mail("NHFC President", "president@northhillsfc.example.com"), ORGS, False) is None


def test_bare_role_from_personal_mailbox():
    assert identities.role_check(_mail("President", "president.club@gmail.com", to="me@gmail.com"), [], False)
    # corresponded before -> not judged on the role alone
    assert identities.role_check(_mail("President", "president.club@gmail.com", to="me@gmail.com"), [], True) is None
    # ordinary job titles in a signature aren't club roles
    assert identities.role_check(_mail("Jane Smith Project Manager", "jane@gmail.com", to="me@gmail.com"),
                                 [], False) is None
    assert identities.role_check(_mail("Jane Smith", "jane@gmail.com", to="me@gmail.com"), [], False) is None


def test_shared_role_forwarding_not_flagged_when_vouched():
    name, sender = "treasurer@riversiderovers.example.org", "treasurer@northhillsfc.example.com"
    assert identities.check(name, sender, [])                                    # nothing vouches -> flag
    assert identities.check(name, sender, [], known_domain=True) is None         # a domain you correspond with
    assert identities.check(name, sender, [], engaged=True) is None              # someone you correspond with
    assert identities.check(name, sender, ORGS) is None                          # sender at a protected domain


def test_cleared_sender_skips_impersonation_but_not_failed_auth():
    class Cur:
        def __init__(self): self.sql = ""
        def execute(self, sql, binds=None): self.sql = sql
        def __iter__(self): return iter([])
        def fetchone(self): return (1,)          # user has overruled a suspicious flag for this sender

    class Conn:
        def cursor(self): return Cur()

    item = dict(id=1, account="me@x", sender_addr="treasurer@northhillsfc.example.com",
                sender_name="treasurer@riversiderovers.example.org", recipients={"to": [{"addr": "me@x"}]},
                subject="Invoice", body="see attached", labels=["INBOX"], meta={}, attachments=[])
    assert triage.security_check(Conn(), item, {}) is None
    spoof = dict(item, meta={"auth": {"spf": "fail", "dkim": "none", "dmarc": "fail"}})
    assert triage.security_check(Conn(), spoof, {}).category == "suspicious"


def test_role_titles_are_not_person_names():
    assert identities.is_person_name("Alex Rivera") and identities.is_person_name("Chris Morgan")
    for n in ("Club Treasurer", "Support Team", "NHFC Registrar", "Accounts", "Riverside Rovers Committee",
              "treasurer@riversiderovers.example.org"):
        assert not identities.is_person_name(n), n


def test_club_treasurer_from_second_club_not_flagged():
    class Cur:
        def execute(self, sql, binds=None): self.sql = sql
        def __iter__(self):
            # if the name check ran, it would find the Riverside treasurer as a known address for this name
            return iter([("treasurer@riversiderovers.example.org",)] if "sender_name" in self.sql else [])
        def fetchone(self): return (0,)

    class Conn:
        def cursor(self): return Cur()

    item = dict(id=1, account="me@x", sender_addr="treasurer@northhillsfc.example.com", sender_name="Club Treasurer",
                recipients={"to": [{"addr": "me@x"}]}, subject="Fees", body="hi", labels=["INBOX"], meta={},
                attachments=[])
    assert triage.security_check(Conn(), item, {}) is None
    # a person's name from a stranger's gmail still gets caught
    person = dict(item, sender_name="Alex Rivera", sender_addr="alex.r.office@gmail.com")

    class Cur2(Cur):
        def __iter__(self):
            return iter([("president@riversiderovers.example.org",)] if "sender_name" in self.sql else [])

    class Conn2:
        def cursor(self): return Cur2()

    p = triage.security_check(Conn2(), person, {})
    assert p and p.category == "suspicious" and "president@riversiderovers.example.org" in p.reasons


def test_ceo_fraud_first_contact_pressure():
    item = dict(id=1, account="me@x", sender_addr="officeadmin-group@freemail.example", sender_name="Alex Rivera",
                recipients={"to": [{"addr": "me@x"}]}, subject="Duty roster",
                body="Hi Jordan, are you available to catch up via email today? Alex", labels=["INBOX"],
                meta={"reply_to": "matt.office.mgmt@outlook.com"}, attachments=[])
    w = triage.first_contact_pressure(item, {"received": 1}, known_domain=False)
    assert w and "first email ever" in w and "different address" in w
    # same words from someone you know are fine
    assert triage.first_contact_pressure(item, {"received": 12, "replied": 3}, known_domain=False) is None
    # role/generic senders aren't judged by this rule
    assert triage.first_contact_pressure(dict(item, sender_name="Club Office"), {}, False) is None


def test_protected_name_flags_the_wp_pl_email():
    w = identities.check("Alex Rivera", "officeadmin-group@freemail.example", PROTECTED)
    assert w and "protected person" in w


def test_lookalike_letters_and_invisible_chars_still_match():
    assert identities.check("Аlex Rivеra", "x@freemail.example", PROTECTED)            # Cyrillic А, е
    assert identities.check("Alex​ Rivera", "x@freemail.example", PROTECTED)     # zero-width space
    assert identities.check("Alex Rivera", "x@freemail.example", PROTECTED)      # non-breaking space


def test_clear_cut_held_back_judgement_calls_reviewed():
    strong = triage._suspicious({"subject": "s"}, ["protected"], strong=True)
    weak = triage._suspicious({"subject": "s"}, ["first contact"], strong=False)
    assert not triage.needs_review(strong, 0.75) and triage.needs_review(weak, 0.75)


def test_held_back_section_renders():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("src/emaild/web/templates"), autoescape=True)
    h = dict(decision_id=7, item_id=1, date="2026-10-07 09:00", sender="Alex Rivera <x@freemail.example>", subject="Duty roster",
             summary="", action="archive", importance="low", category="suspicious", needs_reply=False,
             confidence=0.97, reasons="⚠ Possible phishing", source="security", status="proposed", corrected=None)
    out = env.get_template("triage.html").render(items=[], held=[h], waiting=0, page="triage",
                                                  actions=triage.ACTIONS, importances=triage.IMPORTANCE)
    assert "Held back — 1" in out and "/triage/7/not-phishing" in out and "Duty roster" in out


def test_sign_in_link_sent_with_list_headers_still_detected():
    # Anthropic-style login link: transactional, but sent with List-Unsubscribe like a newsletter
    item = dict(subject="Secure link to log in to Claude.ai",
                body="Click the button below to log in to Claude.ai. This link will expire in 10 minutes.",
                labels=["INBOX", "CATEGORY_UPDATES"], meta={"list_unsubscribe": "<mailto:x>"},
                sender_addr="support@mail.anthropic.com", sender_name="Anthropic",
                recipients={"to": [{"addr": "me@x"}]}, account="me@x", received_dt=datetime(2026, 10, 6, 10, 44))
    p = triage.one_time_check(item)
    assert p and p.category == "one_time" and p.expires_at == datetime(2026, 10, 6, 10, 54)
    # ...but a newsletter that merely mentions a code is still left alone
    promo = dict(item, subject="Your code for 20% off", body="Your code is SAVE20. Offer valid for 3 days.",
                 labels=["INBOX"])
    assert triage.one_time_check(promo) is None


def test_unsafe_filter_drops_one_time_whatever_the_action():
    from emaild.search import EXCLUDE_UNSAFE
    assert "= 'one_time'" in EXCLUDE_UNSAFE.replace("\n", " ")


def test_anthropic_secure_link_subject():
    subj = "Your secure link to Claude.ai is here | 2026-10-06 10:44:26"
    body = "Click the button below to log in to Claude.ai. This link expires in 10 minutes."
    assert onetime.looks_one_time(subj, body) and onetime.looks_sign_in(subj, body)
    assert onetime.expiry_minutes(body, 15) == 10
    assert not onetime.looks_one_time("Your secure payment receipt", "Thanks for your order.")
