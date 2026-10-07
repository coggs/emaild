"""Protected identities: people and organisations that may only send from known addresses or domains.

"Alex Rivera" may only write from president@riversiderovers.example.org or @riversiderovers.example.org; "Riverside Rovers"
only from @riversiderovers.example.org. A display name that matches but comes from anywhere else is impersonation.
"""
from __future__ import annotations

import json
import re

import oracledb

_ADDR_IN_NAME = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
ROLE = re.compile(r"\b(president|vice[- ]?president|treasurer|secretary|registrar|chair(?:person|man|woman)?|"
                  r"committee|board|director|coordinator|manager)\b", re.I)
GENERIC = re.compile(r"\b(club|team|office|admin|administrator|support|info|enquiries|accounts|billing|sales|"
                     r"service|services|help|helpdesk|noreply|no-reply|notifications?|news|newsletter|committee|"
                     r"association|society|council|school|registrations?|membership)\b", re.I)


def is_person_name(name: str) -> bool:
    """A real person's name ("Alex Rivera"), not a role or generic mailbox title ("Club Treasurer", "Support Team")."""
    words = _norm(name).split()
    return 2 <= len(words) <= 4 and not ROLE.search(name or "") and not GENERIC.search(name or "") \
        and "@" not in (name or "")


CLUB_ROLE = re.compile(r"\b(president|vice[- ]?president|treasurer|secretary|registrar|chair(?:person|man|woman)?)\b",
                       re.I)
FREEMAIL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
            "yahoo.com.au", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
            "bigpond.com", "bigpond.net.au", "optusnet.com.au", "mail.com", "zoho.com")


import unicodedata

# Latin lookalikes used to dodge name matching ("Аlex Rivеra" with Cyrillic А, е)
_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "М": "M", "А": "A", "В": "B", "Е": "E", "К": "K", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "Α": "A", "Β": "B", "Ε": "E", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X", "Υ": "Y", "Ζ": "Z"})
_INVISIBLE = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _INVISIBLE.sub("", s).translate(_CONFUSABLE).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w@.\s-]", " ", s)).strip()


def normalise_allowed(values: list[str]) -> list[str]:
    out = []
    for v in values:
        v = v.strip().lower()
        if not v:
            continue
        if "@" not in v:
            v = "@" + v                      # bare domain -> @domain
        out.append(v)
    if not out:
        raise ValueError("give at least one allowed address or @domain")
    return sorted(set(out))


def is_allowed(addr: str, allowed: list[str]) -> bool:
    addr = (addr or "").lower()
    return any(addr == a or (a.startswith("@") and (addr.endswith(a) or addr.endswith("." + a[1:])))
               for a in allowed)


def name_matches(display_name: str, ident: dict) -> bool:
    dn, name = _norm(display_name), _norm(ident["name"])
    if not dn or not name:
        return False
    if ident["kind"] == "person":
        return dn == name or re.search(rf"(^|\W){re.escape(name)}(\W|$)", dn) is not None
    return name in dn                       # organisations: name anywhere in the display name


def check(display_name: str, sender_addr: str, identities: list[dict], engaged: bool = False,
          known_domain: bool = False) -> str | None:
    """Return a warning if this sender impersonates a protected identity, or hides another address in its name."""
    for ident in identities:
        if name_matches(display_name, ident) and not is_allowed(sender_addr, ident["allowed"]):
            where = ", ".join(ident["allowed"][:3])
            return (f"'{display_name}' is a protected {ident['kind']} that only sends from {where}; "
                    f"this came from {sender_addr}")
    m = _ADDR_IN_NAME.search(display_name or "")
    if m and m.group(0).lower() != (sender_addr or "").lower():
        # Shared roles and group forwarding legitimately do this (treasurer@clubA shown, sent by treasurer@clubB).
        # Only suspicious when nothing vouches for the real sender. (DMARC alone doesn't: a lookalike domain
        # passes DMARC for itself.)
        vouched = engaged or known_domain or any(is_allowed(sender_addr, i["allowed"]) for i in identities)
        if not vouched:
            return f"the display name shows {m.group(0)} but the email actually came from {sender_addr}"
    return None


def _domain(addr: str) -> str:
    return (addr or "").lower().rsplit("@", 1)[-1]


def role_check(item: dict, identities: list[dict], engaged: bool) -> str | None:
    """Committee-role impersonation ("President" etc.).

    - tied to a protected organisation (its name in the display name or subject, or sent to an address at its
      domain) but sent from outside its allowed addresses/domains; or
    - a bare role name from a personal mailbox (gmail, outlook...) you've never corresponded with.
    """
    name = item.get("sender_name") or ""
    role = ROLE.search(name)
    if not role:
        return None
    sender = item.get("sender_addr") or ""
    subject = _norm(item.get("subject") or "")
    rcpts = [a.get("addr", "").lower() for k in ("to", "cc") for a in (item.get("recipients") or {}).get(k, [])]
    for ident in identities:
        if ident["kind"] != "org" or is_allowed(sender, ident["allowed"]):
            continue
        org = _norm(ident["name"])
        tied = (org and (org in _norm(name) or org in subject)) or \
            any(is_allowed(r, [a for a in ident["allowed"] if a.startswith("@")]) for r in rcpts)
        if tied:
            return (f"'{name}' claims a {ident['name']} role ({role.group(0)}) but was sent from {sender}, "
                    f"outside {', '.join(ident['allowed'][:2])}")
    if not engaged and _domain(sender) in FREEMAIL and CLUB_ROLE.search(name):
        rest = ROLE.sub("", _norm(name)).replace("the", "").strip(" -.")
        if len(rest.split()) <= 2:   # essentially a role title ("Riverside President"), not a person's full signature
            return f"'{name}' uses a committee role title but was sent from a personal mailbox ({sender})"
    return None


# ---------- DB ----------

def list_all(conn: oracledb.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT id, kind, name, allowed, note FROM protected_identities ORDER BY kind, name")
    return [{"id": r[0], "kind": r[1], "name": r[2],
             "allowed": r[3] if isinstance(r[3], list) else json.loads(r[3] or "[]"), "note": r[4]} for r in cur]


def upsert(conn: oracledb.Connection, name: str, allowed: list[str], kind: str = "person",
           note: str | None = None) -> dict:
    if kind not in ("person", "org"):
        raise ValueError("kind must be person or org")
    allowed = normalise_allowed(allowed)
    name = name.strip()
    conn.cursor().execute("""
        MERGE INTO protected_identities p USING (SELECT :name AS name FROM dual) s ON (p.name = s.name)
        WHEN MATCHED THEN UPDATE SET allowed = :allowed, kind = :kind, note = :note
        WHEN NOT MATCHED THEN INSERT (name, allowed, kind, note) VALUES (:name, :allowed, :kind, :note)""",
        {"name": name, "allowed": json.dumps(allowed), "kind": kind, "note": note})
    return {"name": name, "kind": kind, "allowed": allowed}


def remove(conn: oracledb.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("DELETE FROM protected_identities WHERE LOWER(name) = :1", [name.strip().lower()])
    return cur.rowcount > 0


def suggest(conn: oracledb.Connection, accounts: list[str]) -> dict:
    """Evidence for setting up protection, from the user's own mail:
    role-name senders grouped by the domain they really came from, and the domains mail is addressed to."""
    cur = conn.cursor()
    cur.execute("""SELECT sender_name, LOWER(SUBSTR(sender_addr, INSTR(sender_addr, '@') + 1)) dom, COUNT(*) n
                     FROM items
                    WHERE is_from_me = FALSE AND REGEXP_LIKE(sender_name,
                          'president|treasurer|secretary|registrar|chair|committee', 'i')
                    GROUP BY sender_name, LOWER(SUBSTR(sender_addr, INSTR(sender_addr, '@') + 1))
                    ORDER BY n DESC FETCH FIRST 40 ROWS ONLY""")
    roles = [{"display_name": r[0], "domain": r[1], "count": r[2], "personal_mailbox": r[1] in FREEMAIL}
             for r in cur]
    own = {a.lower() for a in accounts}
    cur.execute("""SELECT LOWER(SUBSTR(r.addr, INSTR(r.addr, '@') + 1)) dom, COUNT(*) n
                     FROM items i, JSON_TABLE(i.recipients, '$.to[*]' COLUMNS (addr VARCHAR2(320) PATH '$.addr')) r
                    WHERE i.is_from_me = FALSE AND r.addr IS NOT NULL
                    GROUP BY LOWER(SUBSTR(r.addr, INSTR(r.addr, '@') + 1)) ORDER BY n DESC FETCH FIRST 25 ROWS ONLY""")
    own_domains = {_domain(a) for a in own}
    addressed = [{"domain": r[0], "count": r[1]} for r in cur
                 if r[0] not in FREEMAIL and r[0] not in own_domains]
    return {"role_senders": roles, "addressed_to_domains": addressed}
