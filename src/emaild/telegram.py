"""Telegram bot: morning brief at EMAILD_BRIEF_TIME, alerts, review from your phone, and questions (ask).

Long polling only (no webhook, nothing exposed). Each Telegram chat links to one emAIl user via a one-time code
from the web app. Telegram is not end-to-end encrypted, so the bot only ever sends summaries (subject, sender,
one-line summary) at the user's chosen detail level - never full emails.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import oracledb

from . import brief as brief_mod
from . import db, recommend, triage
from .config import settings
from .db import UserCtx

log = logging.getLogger(__name__)

DETAIL_LEVELS = ("minimal", "summary", "full")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HELP = ("<b>emAIl</b> — your inbox, without the inbox.\n\n"
        "/brief — a brief of what's come in since the last one\n"
        "/review — triage proposals waiting for you, one card at a time\n"
        "/status — sync and triage at a glance\n"
        "/detail minimal|summary|full — how much the bot shows here\n"
        "/mute, /unmute — pause alerts (the morning brief still comes)\n"
        "/protect Alex Rivera | riversiderovers.example.org — protect a person's name\n"
        "/protectorg NHFC | northhillsfc.example.com — protect an organisation (and its committee roles)\n"
        "/unprotect Name · /protected · /suggest — manage impersonation protection\n"
        "/refresh — re-check the review list against the latest rules\n"
        "/needs — what needs you · /seen — clear it all (or tap 👁 Seen on an alert)\n"
        "/unsubs — lists you could unsubscribe from (🧹 one tap, never automatic)\n"
        "/followups — emails you sent that are still waiting on a reply\n"
        "/rule Anything from Riverside Rovers about the canteen roster goes to Needs attention — a rule in plain "
        "words (read back to you; ✅ Save turns it on)\n"
        "/rules — your rules · /rule off 3 [until February] · /rule on 3 · /rule rm 3 · /rule show 3\n"
        "/rule guidance: I care less about conference marketing unless I'm speaking — soft guidance for the model\n"
        "Anything else you type is a question about your email, e.g. \"last 5 emails from Matt\" (a list) or "
        "\"what are the latest perks from JB Hi-Fi?\" (an answer).")


# ---------- linking (DB) ----------

def create_code(conn: oracledb.Connection) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_codes")
    cur.execute("INSERT INTO telegram_codes (code, expires_at) VALUES (:1, SYSTIMESTAMP + INTERVAL '10' MINUTE)", [code])
    return code


def link_status(conn: oracledb.Connection) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT chat_id, detail_level, muted, linked_at FROM telegram_links")
    r = cur.fetchone()
    return {"chat_id": r[0], "detail_level": r[1], "muted": bool(r[2]), "linked_at": str(r[3])[:16]} if r else None


def set_link(conn: oracledb.Connection, **fields) -> None:
    allowed = {"detail_level", "muted"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "detail_level" in sets and sets["detail_level"] not in DETAIL_LEVELS:
        raise ValueError(f"detail must be one of {DETAIL_LEVELS}")
    if sets:
        conn.cursor().execute("UPDATE telegram_links SET " + ", ".join(f"{k} = :{k}" for k in sets), sets)


def unlink(conn: oracledb.Connection) -> None:
    conn.cursor().execute("DELETE FROM telegram_links")


def claim_code(code: str, chat_id: int) -> UserCtx | None:
    with db.system_session() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT c.tenant_id, c.user_id, u.email FROM telegram_codes c JOIN users u ON u.id = c.user_id
                        WHERE c.code = :1 AND c.expires_at > SYSTIMESTAMP""", [code.strip()])
        r = cur.fetchone()
        if not r:
            return None
        cur.execute("DELETE FROM telegram_links WHERE chat_id = :1", [chat_id])  # a chat links to one user
    ctx = UserCtx(r[0], r[1], r[2])
    with db.user_session(ctx) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM telegram_links")
        cur.execute("INSERT INTO telegram_links (chat_id) VALUES (:1)", [chat_id])
        cur.execute("DELETE FROM telegram_codes")
    return ctx


def all_links() -> list[dict]:
    with db.system_session() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT l.tenant_id, l.user_id, u.email, l.chat_id, l.detail_level, l.muted, l.linked_at
                         FROM telegram_links l JOIN users u ON u.id = l.user_id""")
        return [{"ctx": UserCtx(r[0], r[1], r[2]), "chat_id": r[3], "detail": r[4], "muted": bool(r[5]),
                 "linked_at": r[6]} for r in cur]


def link_for_chat(chat_id: int) -> dict | None:
    return next((l for l in all_links() if l["chat_id"] == chat_id), None)


# ---------- scheduling helpers (pure) ----------

def _hm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def in_quiet_hours(now_local: datetime, spec: str) -> bool:
    if not spec or "-" not in spec:
        return False
    a, b = (_hm(x) for x in spec.split("-", 1))
    t = (now_local.hour, now_local.minute)
    return (a <= t < b) if a <= b else (t >= a or t < b)


def brief_due(now_local: datetime, brief_time: str, days: str, last_morning_local: datetime | None) -> bool:
    if DAYS[now_local.weekday()] not in [d.strip().lower()[:3] for d in days.split(",")]:
        return False
    if (now_local.hour, now_local.minute) < _hm(brief_time):
        return False
    return last_morning_local is None or last_morning_local.date() < now_local.date()


# ---------- rendering ----------

ICON = {"alert": "🔔", "keep": "📌", "archive": "🗄"}


def render_card(g: dict, detail: str) -> str:
    size = f"  ·  ×{g['group_size']} similar" if g.get("group_size", 1) > 1 else ""
    head = f"{ICON.get(g['action'], '')} <b>{g['action']}</b> · {g['importance']} · {g['category']}{size}"
    if detail == "minimal":
        return f"{head}\nFrom {html.escape(g['sender'])}"
    out = [head, f"<b>{html.escape(g['subject'])}</b>", html.escape(g["sender"])]
    if g.get("summary") and g["summary"] != g["subject"]:
        out.append(f"<i>{html.escape(g['summary'][:200])}</i>")
    if detail == "full" and g.get("reasons"):
        out.append(html.escape(g["reasons"][:300]))
    if g.get("first_contact"):
        out.append("⚠️ First email from this address — be careful with links, payments and requests.")
    if g.get("conflict"):
        past = ", ".join(f"{a} {n}×" for a, n in g["past_verdicts"].items())
        out.append(f"⚠️ You've decided this kind both ways before ({past}). Which from now on?")
    return "\n".join(out)


def card_buttons(decision_id: int, chain: bool, item_url: str | None, seen: bool = False) -> dict:
    sfx = ":r" if chain else ""
    rows = [[{"text": "✅ Right", "callback_data": f"v:a:{decision_id}{sfx}"},
             {"text": "🔔 Alert", "callback_data": f"v:l:{decision_id}{sfx}"},
             {"text": "📌 Keep", "callback_data": f"v:k:{decision_id}{sfx}"},
             {"text": "🗄 Archive", "callback_data": f"v:x:{decision_id}{sfx}"}]]
    if seen:
        rows.append([{"text": "👁 Seen", "callback_data": f"s:{decision_id}"}])
    if item_url:
        rows.append([{"text": "🔎 Open original", "url": item_url}])
    return {"inline_keyboard": rows}


UNSUB_HOW = {"one_click": "one-click unsubscribe", "url": "unsubscribe page (you open it)",
             "mailto": "unsubscribe by email (you send it)"}


def render_unsub(r: dict) -> str:
    return (f"🧹 <b>{html.escape(r['sender_name'])}</b>\n{html.escape(r['sender_addr'])}\n"
            f"<i>{html.escape(r['reason'])}</i>\n{UNSUB_HOW.get(r['method'], '')}")


def unsub_buttons(uid: int) -> dict:
    return {"inline_keyboard": [[{"text": "🧹 Unsubscribe", "callback_data": f"u:{uid}"},
                                 {"text": "Keep", "callback_data": f"k:{uid}"}]]}


def render_unsub_result(res: dict) -> str:
    addr = html.escape(res["sender_addr"])
    if res["status"] == "done":
        return f"🧹 Unsubscribed from {addr}."
    if res["status"] == "dismissed":
        return f"📌 Keeping {addr} — won't suggest it again."
    link = res.get("link") or ""
    if res["status"] == "manual":
        return f"Finish it yourself ({html.escape(res['detail'])}):\n<code>{html.escape(link)}</code>"
    return f"✗ {addr}: {html.escape(res['detail'])}" + (f"\nTheir page: <code>{html.escape(link)}</code>" if link else "")


RULE_STATUS = {"pending": "⏳ waiting for you", "active": "✅ on", "paused": "⏸ off", "deleted": "🗑 deleted"}


def render_rule_proposal(rule: dict) -> str:
    """Read-back of a newly compiled rule (generated from the compiled JSON, not the model's prose)."""
    kind = "Guidance" if rule.get("kind") == "guidance" else "Rule"
    v = f" (v{rule['version']})" if rule.get("version", 1) > 1 else ""
    out = [f"📏 <b>{kind} #{rule['id']}{v}: {html.escape(rule.get('name') or '')}</b>",
           html.escape(rule.get("readback") or "")]
    for w in rule.get("warnings") or []:
        out.append(f"⚠️ {html.escape(w)}")
    out.append("Save it?")
    return "\n".join(out)


def rule_proposal_buttons(rule_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Save", "callback_data": f"r:y:{rule_id}"},
                                 {"text": "✖ Cancel", "callback_data": f"r:n:{rule_id}"}]]}


def render_rules_list(rows: list[dict]) -> tuple[str, dict | None]:
    if not rows:
        return ("No rules yet. Try <code>/rule Always archive Strava emails</code>", None)
    lines = ["<b>Your rules</b>"]
    kb = []
    for r in rows[:15]:
        until = f" until {html.escape(r['paused_until'][:10])}" if r["status"] == "paused" and r.get("paused_until") \
            else ""
        fired = f" · fired {r.get('fire_count', 0)}×" if r.get("kind") == "rule" else " · guidance"
        lines.append(f"\n<b>#{r['id']} {html.escape(r.get('name') or '')}</b> · {RULE_STATUS.get(r['status'], '')}"
                     f"{until}{fired}\n{html.escape((r.get('readback') or '')[:300])}")
        row = []
        if r["status"] == "pending":
            row.append({"text": f"✅ Save #{r['id']}", "callback_data": f"r:y:{r['id']}"})
        elif r["status"] == "active":
            row.append({"text": f"⏸ Off #{r['id']}", "callback_data": f"r:p:{r['id']}"})
        elif r["status"] == "paused":
            row.append({"text": f"▶ On #{r['id']}", "callback_data": f"r:o:{r['id']}"})
        row.append({"text": f"🗑 Delete #{r['id']}", "callback_data": f"r:d:{r['id']}"})
        kb.append(row)
    return "\n".join(lines)[:3900], {"inline_keyboard": kb}


def _short_date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b}"


def render_query_result(res: dict, item_url=None) -> tuple[str, dict | None]:
    """Telegram text (+ optional Open buttons) for query.run() output. Only subjects, senders and triage summaries /
    snippets are shown, never bodies; everything is HTML-escaped."""
    from . import query
    interp = f"<i>Interpreted as: {html.escape(res.get('interpreted') or '')}</i>"
    if res["mode"] != "list":
        src = "\n".join(f"[{s['n']}] {html.escape((s.get('date') or '')[:10])} · {html.escape(s.get('from') or '')} — "
                        f"{html.escape(s.get('subject') or '')}" for s in res.get("sources", [])[:6])
        body = html.escape(res.get("answer") or "")[:3300] + (f"\n\n<i>{src}</i>" if src else "")
        return body + "\n\n" + interp, None
    items = res.get("items") or []
    if not items:
        return "📭 Nothing matched.\n\n" + interp, None
    lines = [f"📬 <b>{html.escape(query.headline(res))}</b>"]
    for it in items:
        subj = (it.get("subject") or "(no subject)")[:120]
        summ = (it.get("summary") or "").strip()
        line = f"• {html.escape(_short_date(it.get('date') or ''))} · {html.escape(subj)}"
        if summ and summ != subj:
            line += f" — {html.escape(summ[:160])}"
        if not (res["query"].get("sender_match") or res["query"].get("sender")):
            line += f" <i>({html.escape((it.get('sender') or '')[:60])})</i>"
        lines.append(line)
    text = "\n".join(lines)[:3700] + "\n\n" + interp
    rows = []
    if item_url:
        for n, it in enumerate(items[:5], 1):
            url = item_url(it["item_id"])
            if url:
                rows.append([{"text": f"🔎 Open {n}: {(it.get('subject') or '')[:28]}", "url": url}])
    return text, ({"inline_keyboard": rows} if rows else None)


# ---------- bot ----------

class TelegramAPI:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.Client(timeout=40)

    def call(self, method: str, **params):
        r = self.http.post(self.base + method, json={k: v for k, v in params.items() if v is not None})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: int, text: str, buttons: dict | None = None, reply_to: int | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=buttons,
                         reply_parameters={"message_id": reply_to} if reply_to else None)


class Bot:
    def __init__(self, api: TelegramAPI):
        self.api = api
        self.s = settings()
        self.tz = ZoneInfo(self.s.timezone)
        self.reason_prompts: dict[tuple[int, int], list[int]] = {}   # (chat, prompt msg) -> decision ids

    # ----- helpers -----
    def _item_url(self, item_id: int) -> str | None:
        return f"{self.s.public_url}/item/{item_id}" if self.s.public_url.startswith("https://") else None

    def _group_for(self, conn, decision_id: int) -> list[int]:
        for g in triage.pending_groups(conn, limit=200):
            if decision_id in g["decision_ids"]:
                return g["decision_ids"]
        return [decision_id]

    # ----- commands -----
    def send_brief(self, link: dict, kind: str) -> None:
        with db.user_session(link["ctx"]) as conn:
            b = brief_mod.generate(conn, link["ctx"], kind=kind, delivered_via="telegram")
        self.api.send(link["chat_id"], brief_mod.render_telegram(b, link["detail"], self.s.public_url))

    def send_next_card(self, link: dict, chain: bool = True) -> None:
        with db.user_session(link["ctx"]) as conn:
            groups = triage.pending_groups(conn, limit=1)
            waiting = triage.stats(conn)["waiting_review"]
        if not groups:
            self.api.send(link["chat_id"], "Nothing waiting for review. 🎉")
            return
        g = groups[0]
        text = f"🧐 Review ({waiting} waiting)\n\n" + render_card(g, link["detail"])
        self.api.send(link["chat_id"], text, card_buttons(g["decision_id"], chain, self._item_url(g["item_id"])))

    def send_status(self, link: dict) -> None:
        from . import store
        with db.user_session(link["ctx"]) as conn:
            st, ts = store.status(conn), triage.stats(conn)
        lines = [f"• {html.escape(a['address'])}: {a['items']} msgs, {a['status']}" for a in st["accounts"]]
        agree = f"{ts['agreement']:.0%}" if ts["agreement"] is not None else "—"
        self.api.send(link["chat_id"], "<b>Status</b>\n" + "\n".join(lines) +
                      f"\n\nTriaged {ts['decisions']} · waiting {ts['waiting_review']} · agreement {agree}")

    def answer_question(self, link: dict, question: str) -> None:
        from . import query
        from .llm.router import Router
        self.api.call("sendChatAction", chat_id=link["chat_id"], action="typing")
        with db.user_session(link["ctx"]) as conn:
            res = query.run(conn, question, Router())
        text, buttons = render_query_result(res, self._item_url)
        self.api.send(link["chat_id"], text, buttons)

    def protect(self, link: dict, arg: str, kind: str) -> None:
        from . import identities
        name, _, allowed = arg.partition("|")
        allowed_list = [a for a in re.split(r"[,\s]+", allowed) if a]
        if not name.strip() or not allowed_list:
            self.api.send(link["chat_id"], "Use: <code>/protect Alex Rivera | riversiderovers.example.org</code> or "
                                           "<code>/protectorg NHFC | northhillsfc.example.com</code>")
            return
        try:
            with db.user_session(link["ctx"]) as conn:
                out = identities.upsert(conn, name.strip(), allowed_list, kind)
        except ValueError as e:
            self.api.send(link["chat_id"], html.escape(str(e)))
            return
        sweep = triage.refresh(link["ctx"], days=30)
        self.api.send(link["chat_id"],
                      f"🛡 Protected <b>{html.escape(out['name'])}</b> ({kind}): {html.escape(', '.join(out['allowed']))}"
                      f"\nRe-checked 30 days: {sweep['suspicious']} suspicious, {sweep['spam']} spam.")

    def send_suggestions(self, link: dict) -> None:
        from . import identities
        with db.user_session(link["ctx"]) as conn:
            cur = conn.cursor()
            cur.execute("SELECT address FROM accounts")
            sug = identities.suggest(conn, [r[0] for r in cur])
        lines = ["<b>Committee-role senders</b> (name → where it really came from)"]
        for r in sug["role_senders"][:12]:
            flag = " ⚠️ personal mailbox" if r["personal_mailbox"] else ""
            lines.append(f"{r['count']}× {html.escape(r['display_name'][:40])} → {html.escape(r['domain'])}{flag}")
        lines.append("\n<b>Domains your mail is addressed to</b>")
        lines += [f"{r['count']}× {html.escape(r['domain'])}" for r in sug["addressed_to_domains"][:10]]
        lines.append("\nProtect with <code>/protectorg Name | domain</code>")
        self.api.send(link["chat_id"], "\n".join(lines))

    def send_unsubs(self, link: dict) -> None:
        with db.user_session(link["ctx"]) as conn:
            rows = recommend.suggestions(conn, limit=5)
        if not rows:
            self.api.send(link["chat_id"], "No unsubscribe suggestions. 👌")
            return
        for r in rows:
            self.api.send(link["chat_id"], render_unsub(r), unsub_buttons(r["id"]))

    def send_followups(self, link: dict) -> None:
        with db.user_session(link["ctx"]) as conn:
            rows = recommend.followup_nudges(conn, limit=10)
        if not rows:
            self.api.send(link["chat_id"], "Nobody owes you a reply. 👌")
            return
        for r in rows:
            text = f"⏳ <b>{html.escape(r['to'])}</b> · {int(r['days_waiting'])} days"
            if link["detail"] != "minimal":
                text += f"\n{html.escape(r['subject'])}"
            self.api.send(link["chat_id"], text,
                          {"inline_keyboard": [[{"text": "✓ Done", "callback_data": f"f:{r['item_id']}"}]]})

    def create_rule(self, link: dict, text: str) -> None:
        from . import rules
        from .llm.router import Router
        text = text.strip()
        if not text:
            self.api.send(link["chat_id"], "Use: <code>/rule Always archive Strava emails</code>")
            return
        self.api.call("sendChatAction", chat_id=link["chat_id"], action="typing")
        with db.user_session(link["ctx"]) as conn:
            rule = rules.create(conn, text, Router(), actor="telegram")
        if rule.get("error"):
            self.api.send(link["chat_id"], html.escape(rule["error"]))
            return
        self.api.send(link["chat_id"], render_rule_proposal(rule), rule_proposal_buttons(rule["id"]))

    def send_rules(self, link: dict) -> None:
        from . import rules
        with db.user_session(link["ctx"]) as conn:
            rows = rules.list_rules(conn)
        text, kb = render_rules_list(rows)
        self.api.send(link["chat_id"], text, kb)

    def rule_command(self, link: dict, arg: str) -> None:
        """/rule <text> | off <ref> [until <when>] | on <ref> | rm <ref> | show <ref> | edit <ref> <text>"""
        from . import rules
        chat_id = link["chat_id"]
        sub, _, rest = arg.strip().partition(" ")
        sub = sub.lower()
        if sub not in ("off", "on", "rm", "delete", "show", "edit", "pause"):
            return self.create_rule(link, arg)
        rest = rest.strip()
        until_txt = None
        if sub in ("off", "pause"):
            m = re.match(r"^(.+?)\s+(?:until|till|til)\s+(.+)$", rest, re.I)
            if m:
                rest, until_txt = m.group(1), m.group(2)
        ref, _, text = rest.partition(" ") if sub == "edit" else (rest, "", "")
        with db.user_session(link["ctx"]) as conn:
            r = rules.find_rule(conn, ref) if ref else None
            if r is None:
                self.api.send(chat_id, f"No rule matches “{html.escape(ref)}” — see /rules")
                return
            rid = r["id"]
            if sub in ("off", "pause"):
                until = rules.parse_until(until_txt, datetime.now(self.tz).date()) \
                    if until_txt else None
                if until_txt and until is None:
                    self.api.send(chat_id, f"I can't tell when “{html.escape(until_txt)}” is — try 2026-11-01 or "
                                           f"February.")
                    return
                ok = rules.set_enabled(conn, rid, False, until, actor="telegram")
                msg = f"⏸ Rule #{rid} is off" + (f" until {until}" if until else "") if ok else \
                    f"Rule #{rid} is {rules.status_text(r)}."
            elif sub == "on":
                if r["status"] == "pending":
                    ok = rules.confirm(conn, rid, actor="telegram")["active"]
                else:
                    ok = rules.set_enabled(conn, rid, True, actor="telegram")
                msg = f"▶ Rule #{rid} is on" if ok else f"Rule #{rid} is {rules.status_text(r)}."
            elif sub in ("rm", "delete"):
                msg = f"🗑 Rule #{rid} deleted" if rules.delete(conn, rid, actor="telegram") else "Already deleted."
            elif sub == "show":
                sh = rules.show(conn, rid)
                hist = "\n".join(f"v{v['version']} {html.escape(v['created_at'][:10])}: "
                                 f"{html.escape(v['original_text'][:200])}" for v in sh["history"][-5:])
                msg = (f"📏 <b>#{rid} {html.escape(sh['name'])}</b> · {html.escape(rules.status_text(sh))} · fired "
                       f"{sh['fire_count']}×\n{html.escape(sh['readback'])}\n\n<i>Your words:</i> "
                       f"{html.escape(sh['original_text'])}\n{hist}")
                self.api.send(chat_id, msg[:3900])
                return
            else:
                from .llm.router import Router
                if not text.strip():
                    self.api.send(chat_id, "Use: <code>/rule edit 3 the new wording</code>")
                    return
                new = rules.edit(conn, rid, text, Router(), actor="telegram")
                if new.get("error"):
                    self.api.send(chat_id, html.escape(new["error"]))
                    return
                self.api.send(chat_id, render_rule_proposal(new), rule_proposal_buttons(rid))
                return
        self.api.send(chat_id, msg)

    def rule_intent(self, link: dict, intent: dict) -> None:
        """Free text recognised by rules.parse_intent ("turn off the rugby rule until February")."""
        if intent.get("error"):
            self.api.send(link["chat_id"], html.escape(intent["error"]))
            return
        op = intent["op"]
        if op == "list":
            return self.send_rules(link)
        if op == "add":
            return self.create_rule(link, intent["text"])
        arg = intent["ref"]
        if op == "off" and intent.get("until"):
            arg += f" until {intent['until'].isoformat()}"
        return self.rule_command(link, f"{op} {arg}")

    def _rule_callback(self, q: dict, link: dict, op: str, rule_id: int) -> None:
        from . import rules
        chat_id, msg_id = q["message"]["chat"]["id"], q["message"]["message_id"]
        with db.user_session(link["ctx"]) as conn:
            if op == "y":
                res = rules.confirm(conn, rule_id, actor="telegram")
                ok, label = res["active"], f"✅ Rule #{rule_id} saved and on"
                re_ = res.get("reapplied") or {}
                n = re_.get("updated", 0) + re_.get("retriage", 0)
                if ok and n:
                    label += f" ({n} open decisions re-checked)"
            elif op == "n":
                r = rules.get(conn, rule_id)
                if r and r["version"] > 1:      # an edit: keep the rule, just leave the new version unconfirmed
                    ok, label = True, f"✖ Not saved — rule #{rule_id} stays off until you confirm it (/rules)"
                else:
                    ok, label = rules.delete(conn, rule_id, actor="telegram"), f"✖ Rule #{rule_id} cancelled"
            elif op == "p":
                ok, label = rules.set_enabled(conn, rule_id, False, actor="telegram"), f"⏸ Rule #{rule_id} off"
            elif op == "o":
                ok, label = rules.set_enabled(conn, rule_id, True, actor="telegram"), f"▶ Rule #{rule_id} on"
            else:
                ok, label = rules.delete(conn, rule_id, actor="telegram"), f"🗑 Rule #{rule_id} deleted"
        if not ok:
            self.api.call("answerCallbackQuery", callback_query_id=q["id"], text="Not found (or already done)")
            return
        self.api.call("answerCallbackQuery", callback_query_id=q["id"], text=label[:190])
        if op in ("y", "n"):
            self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                          reply_markup={"inline_keyboard": [[{"text": label[:60], "callback_data": "noop"}]]})
        else:
            self.api.send(chat_id, html.escape(label), reply_to=msg_id)

    def _reco_callback(self, q: dict, link: dict, kind: str, ident: int) -> None:
        chat_id, msg_id = q["message"]["chat"]["id"], q["message"]["message_id"]
        with db.user_session(link["ctx"]) as conn:
            if kind == "f":
                ok = recommend.dismiss_nudge(conn, ident)
                res = None
            else:
                res = recommend.act_id(conn, ident, "unsubscribe" if kind == "u" else "dismiss", actor="telegram")
                ok = res is not None
        if not ok:
            self.api.call("answerCallbackQuery", callback_query_id=q["id"], text="Not found")
            return
        label = "✓ Done" if kind == "f" else {"done": "🧹 Unsubscribed", "dismissed": "📌 Kept",
                                               "manual": "🔗 Link sent", "failed": "✗ Failed"}[res["status"]]
        self.api.call("answerCallbackQuery", callback_query_id=q["id"], text=label)
        self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                      reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})
        if res and res["status"] in ("manual", "failed"):
            self.api.send(chat_id, render_unsub_result(res), reply_to=msg_id)

    # ----- update handling -----
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return self.handle_callback(u["callback_query"])
        msg = u.get("message") or {}
        chat_id, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/link":
            ctx = claim_code(arg, chat_id) if arg else None
            self.api.send(chat_id, f"✅ Linked to {html.escape(ctx.email)}. Your brief arrives at {self.s.brief_time} "
                                   f"({self.s.timezone}).\n\n{HELP}" if ctx else
                          "That code didn't work (they expire after 10 minutes). Get a new one from the Telegram "
                          f"page in emAIl: {self.s.public_url}/telegram")
            return
        link = link_for_chat(chat_id)
        if link is None:
            self.api.send(chat_id, "This chat isn't linked to emAIl yet. Open "
                                   f"{self.s.public_url}/telegram, get a code, and send <code>/link 123456</code>.")
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and (chat_id, reply_to) in self.reason_prompts:
            ids = self.reason_prompts.pop((chat_id, reply_to))
            with db.user_session(link["ctx"]) as conn:
                triage.set_reason(conn, ids, text)
            self.api.send(chat_id, "📝 Thanks — noted. That reason will guide similar emails.")
            return
        if cmd in ("/start", "/help"):
            self.api.send(chat_id, HELP)
        elif cmd == "/brief":
            self.send_brief(link, "on_demand")
        elif cmd == "/review":
            self.send_next_card(link)
        elif cmd == "/status":
            self.send_status(link)
        elif cmd in ("/mute", "/unmute"):
            with db.user_session(link["ctx"]) as conn:
                set_link(conn, muted=(cmd == "/mute"))
            self.api.send(chat_id, "🔕 Alerts paused." if cmd == "/mute" else "🔔 Alerts on.")
        elif cmd == "/detail":
            try:
                with db.user_session(link["ctx"]) as conn:
                    set_link(conn, detail_level=arg.strip().lower())
                self.api.send(chat_id, f"Detail level: {html.escape(arg.strip().lower())}")
            except ValueError:
                self.api.send(chat_id, "Use /detail minimal, /detail summary or /detail full")
        elif cmd in ("/protect", "/protectorg"):
            self.protect(link, arg, "org" if cmd == "/protectorg" else "person")
        elif cmd == "/unprotect":
            from . import identities
            with db.user_session(link["ctx"]) as conn:
                ok = identities.remove(conn, arg)
            self.api.send(chat_id, "Removed." if ok else "Not found — see /protected")
        elif cmd == "/protected":
            from . import identities
            with db.user_session(link["ctx"]) as conn:
                rows = identities.list_all(conn)
            self.api.send(chat_id, "<b>Protected</b>\n" + ("\n".join(
                f"• {html.escape(r['name'])} ({r['kind']}): {html.escape(', '.join(r['allowed']))}" for r in rows)
                or "Nothing yet. Try /suggest"))
        elif cmd == "/suggest":
            self.send_suggestions(link)
        elif cmd == "/refresh":
            r = triage.refresh(link["ctx"])
            with db.user_session(link["ctx"]) as conn:
                waiting = triage.stats(conn)["waiting_review"]
            self.api.send(chat_id, f"🔄 Re-checked {r['checked']}: {r['one_time']} one-time codes, {r['spam']} spam, "
                                   f"{r['suspicious']} suspicious ({r.get('reopened', 0)} reopened), "
                                   f"{r.get('rule', 0)} decided by your rules, {r['cleared']} cleared. "
                                   f"{waiting} waiting — /review")
        elif cmd == "/needs":
            with db.user_session(link["ctx"]) as conn:
                ny = brief_mod.needs_you(conn, days=3)
            lines = [f"🔔 {html.escape(r['sender'])} — {html.escape(r['subject'] or '')}" for r in ny["alerts"]]
            lines += [f"↩️ {html.escape(r['sender'])} — {html.escape(r['subject'] or '')}" for r in ny["awaiting_reply"]]
            self.api.send(chat_id, ("<b>Needs attention</b>\n" + "\n".join(lines) + "\n\n/seen to clear")
                          if lines else "Nothing needs you. 👌")
        elif cmd == "/seen":
            with db.user_session(link["ctx"]) as conn:
                n = brief_mod.dismiss(conn)
            self.api.send(chat_id, f"👁 Cleared {n} from Needs attention." if n else "Nothing to clear.")
        elif cmd == "/unsubs":
            self.send_unsubs(link)
        elif cmd == "/followups":
            self.send_followups(link)
        elif cmd == "/rule":
            self.rule_command(link, arg)
        elif cmd == "/rules":
            self.send_rules(link)
        elif cmd == "/unlink":
            with db.user_session(link["ctx"]) as conn:
                unlink(conn)
            self.api.send(chat_id, "Unlinked. Nothing more will be sent here.")
        elif cmd.startswith("/"):
            self.api.send(chat_id, HELP)
        else:
            from . import rules
            intent = rules.parse_intent(text, datetime.now(self.tz).date())
            if intent:
                self.rule_intent(link, intent)
            else:
                self.answer_question(link, text)

    def handle_callback(self, q: dict) -> None:
        chat_id = q["message"]["chat"]["id"]
        msg_id = q["message"]["message_id"]
        link = link_for_chat(chat_id)
        seen = re.fullmatch(r"s:(\d+)", q.get("data") or "")
        if seen and link is not None:
            with db.user_session(link["ctx"]) as conn:
                brief_mod.dismiss(conn, [int(seen.group(1))])
            self.api.call("answerCallbackQuery", callback_query_id=q["id"], text="👁 Cleared")
            self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                          reply_markup={"inline_keyboard": [[{"text": "👁 Seen", "callback_data": "noop"}]]})
            return
        reco = re.fullmatch(r"([ukf]):(\d+)", q.get("data") or "")
        if reco and link is not None:
            return self._reco_callback(q, link, reco.group(1), int(reco.group(2)))
        rule = re.fullmatch(r"r:([ynpod]):(\d+)", q.get("data") or "")
        if rule and link is not None:
            return self._rule_callback(q, link, rule.group(1), int(rule.group(2)))
        m = re.fullmatch(r"v:([alkx]):(\d+)(:r)?", q.get("data") or "")
        if not m or link is None:
            self.api.call("answerCallbackQuery", callback_query_id=q["id"], text="Not available")
            return
        code, decision_id, chain = m.group(1), int(m.group(2)), bool(m.group(3))
        verdict, corr = ("approve", {}) if code == "a" else \
            ("correct", {"action": {"l": "alert", "k": "keep", "x": "archive"}[code]})
        try:
            with db.user_session(link["ctx"]) as conn:
                ids = self._group_for(conn, decision_id)
                res = triage.review_many(conn, ids, verdict, corr, None)
        except ValueError as e:
            self.api.call("answerCallbackQuery", callback_query_id=q["id"], text=str(e)[:150])
            return
        done = "✅ Approved" if res["status"] == "approved" else f"✏️ Set to {corr['action']}"
        n = f" ({res['reviewed']} emails)" if res["reviewed"] > 1 else ""
        self.api.call("answerCallbackQuery", callback_query_id=q["id"], text=done + n)
        self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                      reply_markup={"inline_keyboard": [[{"text": done + n, "callback_data": "noop"}]]})
        if verdict == "correct":
            p = self.api.send(chat_id, "Why? <i>Reply to this message with a short reason (optional) — "
                                       "it's the fastest way to teach emAIl.</i>", reply_to=msg_id)
            self.reason_prompts[(chat_id, p["message_id"])] = res["decision_ids"]
        if chain:
            self.send_next_card(link)

    # ----- scheduled work -----
    def scheduled(self) -> None:
        now_local = datetime.now(self.tz)
        for link in all_links():
            try:
                self._maybe_brief(link, now_local)
                if not link["muted"] and not in_quiet_hours(now_local, self.s.quiet_hours):
                    self._push_alerts(link)
            except Exception:
                log.exception("scheduled work failed for user %s", link["ctx"].user_id)

    def push_codes(self) -> None:
        """Sign-in codes/links: sent as soon as they arrive, regardless of quiet hours (you asked for them)."""
        for link in all_links():
            if link["muted"]:
                continue
            try:
                self._push_codes(link)
            except Exception:
                log.exception("code push failed for user %s", link["ctx"].user_id)

    def _push_codes(self, link: dict) -> None:
        from . import onetime
        msgs = []
        with db.user_session(link["ctx"]) as conn:
            cur = conn.cursor()
            cur.execute("""SELECT d.id, i.sender_name, i.sender_addr, i.subject, NVL(i.body_text, i.full_text),
                                  ROUND((CAST(d.expires_at AS DATE) - CAST(SYSTIMESTAMP AS DATE)) * 1440), d.expires_at
                             FROM decisions d JOIN items i ON i.id = d.item_id
                            WHERE d.category = 'one_time' AND d.notified_at IS NULL
                              AND d.expires_at > SYSTIMESTAMP AND d.created_at > :linked""",
                        {"linked": link["linked_at"]})
            rows = cur.fetchall()
            for did, name, addr, subject, body, mins, exp in rows:
                local = exp.replace(tzinfo=timezone.utc).astimezone(self.tz).strftime("%H:%M") if exp else "?"
                text = (f"🔑 <b>Sign-in code / link</b> from {html.escape(name or addr or '')}\n"
                        f"{html.escape(subject or '')}\nExpires {local} (in {int(mins or 0)} min) — open Gmail to use it.")
                if self.s.telegram_show_codes and link["detail"] != "minimal":
                    code = onetime.extract_code(body or "")
                    if code:
                        text += f"\nCode: <code>{html.escape(code)}</code>"
                msgs.append(text)
                cur.execute("UPDATE decisions SET notified_at = SYSTIMESTAMP WHERE id = :1", [did])
        for t in msgs:
            self.api.send(link["chat_id"], t)

    def _maybe_brief(self, link: dict, now_local: datetime) -> None:
        with db.user_session(link["ctx"]) as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(created_at) FROM briefs WHERE kind = 'morning'")
            last = cur.fetchone()[0]
        last_local = last.replace(tzinfo=timezone.utc).astimezone(self.tz) if last else None
        if brief_due(now_local, self.s.brief_time, self.s.brief_days, last_local):
            log.info("sending morning brief to user %s", link["ctx"].user_id)
            self.send_brief(link, "morning")

    def _push_alerts(self, link: dict) -> None:
        with db.user_session(link["ctx"]) as conn:
            cur = conn.cursor()
            cur.execute(f"""SELECT d.id FROM decisions d
                             WHERE {brief_mod.FINAL_ACTION} = 'alert' AND d.notified_at IS NULL
                               AND d.created_at > :linked AND d.created_at > SYSTIMESTAMP - INTERVAL '1' DAY
                             ORDER BY d.created_at FETCH FIRST 5 ROWS ONLY""", {"linked": link["linked_at"]})
            ids = [r[0] for r in cur]
            cards = []
            for did in ids:
                cur.execute("SELECT item_id FROM decisions WHERE id = :1", [did])
                item_id = cur.fetchone()[0]
                d = triage.decision_for_item(conn, item_id)
                from . import senders
                item = triage.load_item(conn, item_id)
                st = senders.get(conn, item["sender_addr"]) if item else {}
                first = st.get("received", 0) <= 1 and not st.get("replied") and not st.get("sent_to")
                cards.append({**d, "group_size": 1, "past_verdicts": {}, "conflict": False, "first_contact": first})
                cur.execute("UPDATE decisions SET notified_at = SYSTIMESTAMP WHERE id = :1", [did])
        for d in cards:
            self.api.send(link["chat_id"], "🔔 <b>New</b>\n" + render_card(d, link["detail"]),
                          card_buttons(d["decision_id"], False, self._item_url(d["item_id"]), seen=True))


def run() -> None:
    s = settings()
    if not s.telegram_token:
        log.error("EMAILD_TELEGRAM_TOKEN is not set; the Telegram service is idle")
        while True:
            time.sleep(3600)
    api = TelegramAPI(s.telegram_token)
    bot = Bot(api)
    me = api.call("getMe")
    log.info("telegram bot @%s running (brief %s %s, quiet %s)", me.get("username"), s.brief_time, s.timezone,
             s.quiet_hours)
    pending = api.call("getUpdates", offset=-1, timeout=0)        # skip anything queued while we were down
    offset = pending[-1]["update_id"] + 1 if pending else None
    last_sched = 0.0
    while True:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=10, allowed_updates=["message", "callback_query"])
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    bot.handle_update(u)
                except Exception:
                    log.exception("handling update %s failed", u.get("update_id"))
            bot.push_codes()                      # every ~10 s: codes are time-critical
            if time.monotonic() - last_sched > 30:
                bot.scheduled()
                last_sched = time.monotonic()
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("telegram polling error: %s", e)
            time.sleep(5)
