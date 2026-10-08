"""FastAPI app: health, status, Gmail / Outlook.com account linking, and the (Phase 0) placeholder dashboard.

Phase 0 has no sign-in: it binds to localhost and acts as EMAILD_DEFAULT_USER. OIDC sign-in arrives with multi-user.
"""
from __future__ import annotations

import base64
import html
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from google_auth_oauthlib.flow import Flow

from .. import brief as brief_mod
from .. import crypto, db, projects, recommend, show, store, telegram, threads, trackers, triage, users
from ..channels import outlook
from ..channels.gmail import SCOPES, GmailChannel
from ..config import settings

log = logging.getLogger(__name__)
app = FastAPI(title="emAIl", version="0.0.1")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_pending: dict[str, tuple[float, Flow, str]] = {}  # state -> (created, flow, user email)
# state -> (created, PKCE verifier, user email, kind 'personal'|'work', authority tenant)
_ms_pending: dict[str, tuple[float, str, str, str, str]] = {}
PENDING_SECONDS = 900


def _prune(pending: dict) -> None:
    now = time.time()
    for k in [k for k, v in pending.items() if now - v[0] > PENDING_SECONDS]:
        pending.pop(k, None)

_OPEN_PATHS = {"/healthz"}


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """Phase 0 gate for LAN access: HTTP Basic auth against EMAILD_WEB_PASSWORD (any username).

    Replaced by OIDC sign-in when multi-user lands. Without a password set, the app should stay on localhost.
    """
    password = settings().web_password
    if not password or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            given = ""
        if hmac.compare_digest(given.encode(), password.encode()):
            return await call_next(request)
    return Response("authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="emAIl", charset="UTF-8"'})


@app.get("/healthz")
def healthz() -> dict:
    with db.pool().acquire() as conn:
        conn.cursor().execute("SELECT 1 FROM dual")
    return {"ok": True}


@app.get("/api/status")
def api_status() -> JSONResponse:
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        return JSONResponse(store.status(conn))


def _page_data(conn, ctx) -> dict:
    tstats = triage.stats(conn)
    return {"status": store.status(conn), "tstats": tstats, "waiting": tstats["waiting_review"],
            "needs": brief_mod.needs_you(conn, days=3), "codes": brief_mod.active_codes(conn),
            "reco": recommend.counts(conn, ctx.user_id),   # cached 10 min: this panel refreshes every 30 s
            "trackers_line": trackers.home_line(conn, ctx.user_id),   # cached 5 min; '' before migration 014
            "projects_line": projects.home_line(conn, ctx.user_id)}   # cached 5 min; '' before migration 015


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        data = _page_data(conn, ctx)
    return templates.TemplateResponse(request, "index.html", {"user": ctx.email, "page": "home",
                                                              "ms_work": bool(settings().ms_client_id), **data})


@app.get("/fragments/status", response_class=HTMLResponse)
def status_fragment(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        data = _page_data(conn, ctx)
    return templates.TemplateResponse(request, "status_fragment.html", data)


@app.post("/needs/clear", response_class=HTMLResponse)
def needs_clear(request: Request):
    """Mark everything in Needs attention as seen, then re-render the status panel."""
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        brief_mod.dismiss(conn)
        data = _page_data(conn, ctx)
    return templates.TemplateResponse(request, "status_fragment.html", data)


@app.post("/needs/{decision_id}/seen", response_class=HTMLResponse)
def needs_seen(decision_id: int):
    with db.user_session(users.resolve()) as conn:
        brief_mod.dismiss(conn, [decision_id])
    return HTMLResponse("")


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations_page(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        unsubs = recommend.suggestions(conn)
        followups = recommend.followup_nudges(conn, limit=20)
        waiting = triage.stats(conn)["waiting_review"]
    return templates.TemplateResponse(request, "recommendations.html", {
        "unsubs": unsubs, "followups": followups, "page": "recommendations", "waiting": waiting,
        "days_min": 3, "days_max": 21})


def _unsub_result(uid: int, res: dict | None) -> HTMLResponse:
    if res is None:
        return HTMLResponse(f'<div class="li err" id="u{uid}">Not found</div>')
    macro = templates.env.get_template("_reco.html").module.unsub_result
    return HTMLResponse(str(macro(uid, res)))


@app.post("/recommendations/unsub/{uid}", response_class=HTMLResponse)
def recommendations_unsub(uid: int):
    """User pressed Unsubscribe (one-click POST) or opened a manual link (recorded as 'manual')."""
    with db.user_session(users.resolve()) as conn:
        return _unsub_result(uid, recommend.act_id(conn, uid, "unsubscribe", actor="web"))


@app.post("/recommendations/keep/{uid}", response_class=HTMLResponse)
def recommendations_keep(uid: int):
    with db.user_session(users.resolve()) as conn:
        return _unsub_result(uid, recommend.act_id(conn, uid, "dismiss", actor="web"))


@app.post("/recommendations/followup/{item_id}/dismiss", response_class=HTMLResponse)
def followup_dismiss(item_id: int):
    with db.user_session(users.resolve()) as conn:
        recommend.dismiss_nudge(conn, item_id)
    return HTMLResponse("")


@app.get("/triage", response_class=HTMLResponse)
def triage_page(request: Request, limit: int = 30):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        groups = triage.pending_groups(conn, max(1, min(limit, 100)))
        waiting = triage.stats(conn)["waiting_review"]
        held = triage.held_back(conn)
    return templates.TemplateResponse(request, "triage.html", {
        "items": groups, "waiting": waiting, "page": "triage", "held": held,
        "actions": triage.ACTIONS, "importances": triage.IMPORTANCE})


@app.post("/triage/refresh")
def triage_refresh():
    triage.refresh(users.resolve())
    return RedirectResponse("/triage", status_code=303)


@app.post("/triage/{decision_id}/not-phishing", response_class=HTMLResponse)
def not_phishing(decision_id: int):
    with db.user_session(users.resolve()) as conn:
        triage.review(conn, decision_id, "correct", {"action": "keep", "category": "other"},
                      "Not phishing (marked from Held back)")
    return HTMLResponse(f'<div class="done" id="h{decision_id}">✓ restored — this sender is trusted from now on</div>')


@app.post("/triage/{decision_id}", response_class=HTMLResponse)
def triage_verdict(decision_id: int, verdict: str = Form(...), action: str = Form(""), importance: str = Form(""),
                   reason: str = Form(""), ids: str = Form("")):
    ctx = users.resolve()
    corrections = {"action": action or None, "importance": importance or None}
    if verdict == "approve" and (action or importance):
        verdict = "correct"  # picked values then hit the main button: treat as a correction
    try:
        group = [decision_id] + [int(x) for x in ids.split(",") if x.strip().isdigit()]
    except ValueError:
        group = [decision_id]
    try:
        with db.user_session(ctx) as conn:
            res = triage.review_many(conn, group, verdict, corrections, reason.strip() or None)
    except ValueError as e:
        return HTMLResponse(f'<div class="card err" id="d{decision_id}">{html.escape(str(e))}</div>', status_code=200)
    n = res["reviewed"]
    what = "approved" if res["status"] == "approved" else "corrected: " + html.escape(
        ", ".join(f"{k} → {v}" for k, v in res["corrected"].items()))
    return HTMLResponse(f'<div class="done" id="d{decision_id}">✓ {what}{f" ({n} emails)" if n > 1 else ""}</div>')


# ---------- rules ----------

def _rules_macro(name: str):
    return getattr(templates.env.get_template("_rules.html").module, name)


def _rule_row(conn, rule_id: int) -> HTMLResponse:
    from .. import rules
    r = rules.get(conn, rule_id)
    if r is None or r["status"] == "deleted":
        return HTMLResponse("")
    return HTMLResponse(str(_rules_macro("rule_row")(r)))


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    from .. import rules
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        rows = rules.list_rules(conn)
        waiting = triage.stats(conn)["waiting_review"]
        suggestions = rules.list_suggestions(conn, key=ctx.user_id)
    return templates.TemplateResponse(request, "rules.html", {"rules": rows, "page": "rules", "waiting": waiting,
                                                              "suggestions": suggestions})


@app.post("/rules", response_class=HTMLResponse)
def rules_add(text: str = Form("")):
    """Compile the user's words; show the read-back with Save / Cancel (the rule is pending until Save)."""
    from .. import rules
    from ..llm.router import Router
    text = text.strip()[:2000]
    if not text:
        return HTMLResponse("")
    router = Router()
    with db.user_session(users.resolve()) as conn:
        r = rules.create(conn, text, router, actor="web")
        if not r.get("error"):
            r["dry_run"] = rules.dry_run_safe(conn, r, router)
    if r.get("error"):
        return HTMLResponse(f'<div class="card err">{html.escape(r["error"])}</div>')
    return HTMLResponse(str(_rules_macro("proposal")(r)))


@app.post("/rules/{rule_id}/test", response_class=HTMLResponse)
def rules_test(rule_id: int, days: int = Form(30)):
    """Dry run of an existing rule over recent mail (nothing changes)."""
    from .. import rules
    from ..llm.router import Router
    with db.user_session(users.resolve()) as conn:
        res = rules.dry_run_ref(conn, str(int(rule_id)), days=days, router=Router())
    if res.get("error"):
        return HTMLResponse(f'<div class="err" id="t{rule_id}">{html.escape(res["error"])}</div>')
    return HTMLResponse(str(_rules_macro("dry_run")(res["dry_run"], f"t{rule_id}")))


@app.post("/rules/suggestions/{sid}/accept", response_class=HTMLResponse)
def rules_suggestion_accept(sid: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        res = rules.accept_suggestion(conn, sid, actor="web")
        if res.get("error"):
            return HTMLResponse(f'<div class="li err" id="s{sid}">{html.escape(res["error"])}</div>')
    r = res["rule"]
    return HTMLResponse(f'<div class="done" id="s{sid}">✅ Saved as rule #{int(r["id"])} and turned on: '
                        f'{html.escape(r.get("readback") or "")}</div>')


@app.post("/rules/suggestions/{sid}/dismiss", response_class=HTMLResponse)
def rules_suggestion_dismiss(sid: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        rules.dismiss_suggestion(conn, sid, actor="web")
    return HTMLResponse(f'<div class="done" id="s{sid}">✖ Won\'t suggest that again</div>')


@app.post("/rules/{rule_id}/confirm", response_class=HTMLResponse)
def rules_confirm(rule_id: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        rules.confirm(conn, rule_id, actor="web")
        return _rule_row(conn, rule_id)


@app.post("/rules/{rule_id}/cancel", response_class=HTMLResponse)
def rules_cancel(rule_id: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        r = rules.get(conn, rule_id)
        if r and r["status"] == "pending" and r["version"] == 1:
            rules.delete(conn, rule_id, actor="web")
            return HTMLResponse('<div class="done">✖ Cancelled</div>')
        return _rule_row(conn, rule_id)


@app.post("/rules/{rule_id}/off", response_class=HTMLResponse)
def rules_off(rule_id: int, until: str = Form("")):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .. import rules
    when = None
    if until.strip():
        when = rules.parse_until(until, datetime.now(ZoneInfo(settings().timezone)).date())
        if when is None:
            return HTMLResponse(f'<div class="li err" id="r{rule_id}">Can\'t read “{html.escape(until[:40])}” as a '
                                f'date — try 2026-11-01 or February. <a href="/rules">Back</a></div>')
    with db.user_session(users.resolve()) as conn:
        rules.set_enabled(conn, rule_id, False, when, actor="web")
        return _rule_row(conn, rule_id)


@app.post("/rules/{rule_id}/on", response_class=HTMLResponse)
def rules_on(rule_id: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        rules.set_enabled(conn, rule_id, True, actor="web")
        return _rule_row(conn, rule_id)


@app.post("/rules/{rule_id}/delete", response_class=HTMLResponse)
def rules_delete(rule_id: int):
    from .. import rules
    with db.user_session(users.resolve()) as conn:
        rules.delete(conn, rule_id, actor="web")
    return HTMLResponse("")


# ---------- trackers (F5) ----------

def _trackers_macro(name: str):
    return getattr(templates.env.get_template("_trackers.html").module, name)


def _board(conn, tracker_id: int) -> HTMLResponse:
    b = trackers.board(conn, tracker_id)
    if b is None or b["tracker"]["status"] == "deleted":
        return HTMLResponse("")
    return HTMLResponse(str(_trackers_macro("board")(b)))


@app.post("/trackers/items/{item_id}/delivered", response_class=HTMLResponse)
def trackers_item_delivered(item_id: int):
    """Mark delivered (board button): the order leaves the board; the board is re-rendered."""
    with db.user_session(users.resolve()) as conn:
        res = trackers.mark_item(conn, item_id, "delivered", actor="web")
        if res.get("error"):
            return HTMLResponse(f'<div class="err">{html.escape(res["error"])}</div>')
        return _board(conn, res["tracker_id"])


@app.post("/trackers/{tracker_id}/clear-old", response_class=HTMLResponse)
def trackers_clear_old(tracker_id: int):
    """Close the orders nobody has heard about for CLEAR_OLD_DAYS days (assumed delivered; a later email reopens)."""
    with db.user_session(users.resolve()) as conn:
        try:
            trackers.clear_old(conn, tracker_id, actor="web")
        except store.MigrationNeeded as e:
            return HTMLResponse(f'<div class="err" id="tr{int(tracker_id)}">{html.escape(str(e))}</div>')
        return _board(conn, tracker_id)


@app.get("/trackers", response_class=HTMLResponse)
def trackers_page(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        boards = trackers.boards(conn)
        waiting = triage.stats(conn)["waiting_review"]
        suggestions = trackers.list_suggestions(conn, key=ctx.user_id)
    return templates.TemplateResponse(request, "trackers.html", {"boards": boards, "page": "trackers",
                                                                 "waiting": waiting, "suggestions": suggestions})


@app.post("/trackers", response_class=HTMLResponse)
def trackers_add(text: str = Form("")):
    """Compile the user's words; show the read-back and dry run with Save / Cancel (pending until Save)."""
    from ..llm.router import Router
    text = text.strip()[:2000]
    if not text:
        return HTMLResponse("")
    router = Router()
    with db.user_session(users.resolve()) as conn:
        t = trackers.create(conn, text, router, actor="web")
        if not t.get("error"):
            t["dry_run"] = trackers.dry_run_safe(conn, t, router)
    if t.get("error"):
        return HTMLResponse(f'<div class="card err">{html.escape(t["error"])}</div>')
    return HTMLResponse(str(_trackers_macro("proposal")(t)))


@app.post("/trackers/{tracker_id}/test", response_class=HTMLResponse)
def trackers_test(tracker_id: int):
    from ..llm.router import Router
    with db.user_session(users.resolve()) as conn:
        res = trackers.dry_run_ref(conn, str(int(tracker_id)), router=Router())
    if res.get("error"):
        return HTMLResponse(f'<div class="err" id="tt{tracker_id}">{html.escape(res["error"])}</div>')
    return HTMLResponse(str(_trackers_macro("dry_run")(res["dry_run"], f"tt{tracker_id}")))


@app.post("/trackers/suggestions/{sid}/accept", response_class=HTMLResponse)
def trackers_suggestion_accept(sid: int):
    with db.user_session(users.resolve()) as conn:
        res = trackers.accept_suggestion(conn, sid, actor="web")
    if res.get("error"):
        return HTMLResponse(f'<div class="li err" id="ts{sid}">{html.escape(res["error"])}</div>')
    t = res["tracker"]
    return HTMLResponse(f'<div class="done" id="ts{sid}">✅ Tracking as #{int(t["id"])}: '
                        f'{html.escape(t.get("readback") or "")}</div>')


@app.post("/trackers/suggestions/{sid}/dismiss", response_class=HTMLResponse)
def trackers_suggestion_dismiss(sid: int):
    with db.user_session(users.resolve()) as conn:
        trackers.dismiss_suggestion(conn, sid, actor="web")
    return HTMLResponse(f'<div class="done" id="ts{sid}">✖ Won\'t suggest that again</div>')


@app.post("/trackers/{tracker_id}/confirm", response_class=HTMLResponse)
def trackers_confirm(tracker_id: int):
    with db.user_session(users.resolve()) as conn:
        trackers.confirm(conn, tracker_id, actor="web")
        return _board(conn, tracker_id)


@app.post("/trackers/{tracker_id}/cancel", response_class=HTMLResponse)
def trackers_cancel(tracker_id: int):
    with db.user_session(users.resolve()) as conn:
        t = trackers.get(conn, tracker_id)
        if t and t["status"] == "pending" and t["version"] == 1:
            trackers.delete(conn, tracker_id, actor="web")
            return HTMLResponse('<div class="done">✖ Cancelled</div>')
        return _board(conn, tracker_id)


@app.post("/trackers/{tracker_id}/{op}", response_class=HTMLResponse)
def trackers_op(tracker_id: int, op: str):
    if op not in ("on", "off", "delete"):
        raise HTTPException(404, "unknown operation")
    with db.user_session(users.resolve()) as conn:
        if op == "delete":
            trackers.delete(conn, tracker_id, actor="web")
            return HTMLResponse("")
        trackers.set_enabled(conn, tracker_id, op == "on", actor="web")
        return _board(conn, tracker_id)


# ---------- projects (Phase 3) ----------

def _projects_macro(name: str):
    return getattr(templates.env.get_template("_projects.html").module, name)


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        rows = projects.overview_rows(conn)
        waiting = triage.stats(conn)["waiting_review"]
        suggestions = projects.list_suggestions(conn)
    return templates.TemplateResponse(request, "projects.html", {"rows": rows, "page": "projects",
                                                                 "waiting": waiting, "suggestions": suggestions})


@app.post("/projects", response_class=HTMLResponse)
def projects_add(text: str = Form(""), parent: str = Form(""), item_id: str = Form("")):
    """Compile the user's words; show the read-back and dry run with Save / Cancel (pending until Save)."""
    from ..llm.router import Router
    text = text.strip()[:2000]
    iid = int(item_id) if item_id.strip().isdigit() else None
    if not text and iid is None:
        return HTMLResponse("")
    router = Router()
    with db.user_session(users.resolve()) as conn:
        p = projects.create(conn, text, router, actor="web", parent=parent.strip() or None, item_id=iid)
        if not p.get("error"):
            p["dry_run"] = projects.dry_run_safe(conn, p, router)
    if p.get("error"):
        return HTMLResponse(f'<div class="card err">{html.escape(p["error"])}</div>')
    return HTMLResponse(str(_projects_macro("proposal")(p)))


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: int):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        p = projects.get(conn, project_id)
        if p is None or p["status"] == "deleted":
            raise HTTPException(404, "not found")
        st = projects.project_status(conn, p)
        others = [r for r in projects.list_projects(conn) if r["id"] != p["id"]]
        waiting = triage.stats(conn)["waiting_review"]
    return templates.TemplateResponse(request, "project.html", {"p": p, "st": st, "others": others,
                                                                "page": "projects", "waiting": waiting})


@app.post("/projects/{project_id}/overview", response_class=HTMLResponse)
def project_overview(project_id: int):
    """Gemma's 2-3 sentence overview, written from the facts only (on demand: it's a model call)."""
    from ..llm.router import Router
    with db.user_session(users.resolve()) as conn:
        st = projects.project_status(conn, project_id, Router(), with_overview=True)
    text = st.get("overview") or ("Nothing to summarise yet." if not st.get("error") else st["error"])
    return HTMLResponse(f'<p class="overview" id="ov">{html.escape(text)}</p>')


@app.post("/projects/{project_id}/confirm", response_class=HTMLResponse)
def projects_confirm(project_id: int):
    with db.user_session(users.resolve()) as conn:
        res = projects.confirm(conn, project_id, actor="web")
        p = projects.get(conn, project_id)
    if not res["active"] or p is None:
        return HTMLResponse('<div class="err">Not saved (it may have been cancelled).</div>')
    extra = f" {int(res['linked'])} emails from the thread filed." if res.get("linked") else ""
    return HTMLResponse(f'<div class="done" id="pr{int(project_id)}">✅ Saved: <a href="/projects/{int(project_id)}">'
                        f'{html.escape(p["name"])}</a>.{extra} Matching emails from the last '
                        f'{projects.WINDOW_DAYS} days are filed over the next few minutes.</div>')


@app.post("/projects/{project_id}/cancel", response_class=HTMLResponse)
def projects_cancel(project_id: int):
    with db.user_session(users.resolve()) as conn:
        p = projects.get(conn, project_id)
        if p and p["status"] == "pending":
            projects.delete(conn, project_id, actor="web")
    return HTMLResponse('<div class="done">✖ Cancelled</div>')


@app.post("/projects/{project_id}/move")
def projects_move(project_id: int, parent: str = Form("")):
    with db.user_session(users.resolve()) as conn:
        res = projects.move(conn, project_id, parent.strip() or None, actor="web")
    if res.get("error"):
        return HTMLResponse(f'<div class="err">{html.escape(res["error"])}</div>', status_code=400)
    return RedirectResponse(f"/projects/{int(project_id)}", status_code=303)


@app.post("/projects/{project_id}/{op}")
def projects_op(project_id: int, op: str):
    status = {"done": "done", "archive": "archived", "reopen": "active", "delete": "deleted"}.get(op)
    if status is None:
        raise HTTPException(404, "unknown operation")
    with db.user_session(users.resolve()) as conn:
        projects.set_status(conn, project_id, status, actor="web")
    return RedirectResponse("/projects" if op == "delete" else f"/projects/{int(project_id)}", status_code=303)


@app.post("/projects/suggestions/{sid}/accept", response_class=HTMLResponse)
def projects_suggestion_accept(sid: int):
    with db.user_session(users.resolve()) as conn:
        res = projects.accept_suggestion(conn, sid, actor="web")
    if res.get("error"):
        return HTMLResponse(f'<div class="li err" id="ps{sid}">{html.escape(res["error"])}</div>')
    p = res["project"]
    return HTMLResponse(f'<div class="done" id="ps{sid}">✅ Created <a href="/projects/{int(p["id"])}">'
                        f'{html.escape(p["name"])}</a></div>')


@app.post("/projects/suggestions/{sid}/dismiss", response_class=HTMLResponse)
def projects_suggestion_dismiss(sid: int):
    with db.user_session(users.resolve()) as conn:
        projects.dismiss_suggestion(conn, sid, actor="web")
    return HTMLResponse(f'<div class="done" id="ps{sid}">✖ Won\'t suggest that again</div>')


@app.post("/item/{item_id}/project", response_class=HTMLResponse)
def item_add_to_project(item_id: int, project: str = Form("")):
    """'Add to project' on an email: files it (and its thread's later replies) under the chosen project."""
    if not project.strip():
        return HTMLResponse('<span class="err">Pick a project.</span>')
    with db.user_session(users.resolve()) as conn:
        res = projects.link(conn, item_id, project.strip(), actor="web")
    if res.get("error"):
        return HTMLResponse(f'<span class="err">{html.escape(res["error"])}</span>')
    return HTMLResponse(f'<span class="done">📁 Filed under <a href="/projects/{int(res["project_id"])}">'
                        f'{html.escape(res["project"])}</a></span>')


@app.post("/item/{item_id}/thread-status", response_class=HTMLResponse)
def item_thread_status(item_id: int):
    from ..llm.router import Router
    with db.user_session(users.resolve()) as conn:
        ts = projects.thread_status(conn, item_id, router=Router())
    if ts.get("error"):
        return HTMLResponse(f'<div class="err" id="ts">{html.escape(ts["error"])}</div>')
    return HTMLResponse('<div class="card" id="ts">' + "<br>".join(html.escape(x) for x in projects.thread_lines(ts))
                        + "</div>")


@app.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        b = brief_mod.last_brief(conn)
        waiting = triage.stats(conn)["waiting_review"]
    nums = brief_mod.number_map(b) if b else {}       # the same [n] order as the Telegram brief
    return templates.TemplateResponse(request, "brief.html", {"b": b, "page": "brief", "waiting": waiting,
                                                              "nums": nums})


@app.post("/brief")
def brief_generate():
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        brief_mod.generate(conn, ctx, kind="on_demand", delivered_via="web")
    return RedirectResponse("/brief", status_code=303)


@app.get("/telegram", response_class=HTMLResponse)
def telegram_page(request: Request):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        link = telegram.link_status(conn)
        waiting = triage.stats(conn)["waiting_review"]
    s = settings()
    return templates.TemplateResponse(request, "telegram.html", {
        "link": link, "page": "telegram", "waiting": waiting, "configured": bool(s.telegram_token),
        "brief_time": s.brief_time, "tz": s.timezone, "levels": telegram.DETAIL_LEVELS})


@app.post("/telegram/code", response_class=HTMLResponse)
def telegram_code():
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        code = telegram.create_code(conn)
    return HTMLResponse(f'<div class="card">Send this to your emAIl bot in Telegram within 10 minutes:'
                        f'<pre class="body" style="font-size:22px;margin:8px 0">/link {code}</pre></div>')


@app.post("/telegram/settings")
def telegram_settings(detail_level: str = Form(""), action: str = Form("")):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        if action == "unlink":
            telegram.unlink(conn)
        elif detail_level:
            telegram.set_link(conn, detail_level=detail_level)
    return RedirectResponse("/telegram", status_code=303)


@app.post("/ask", response_class=HTMLResponse)
def ask_box(request: Request, question: str = Form("")):
    """Home-page "Ask your email" box: query understanding, then a list or a cited answer (HTMX fragment)."""
    from .. import query
    from ..llm.router import Router
    question = question.strip()[:500]
    if not question:
        return HTMLResponse("")
    ctx = users.resolve()
    try:
        with db.user_session(ctx) as conn:
            res = query.run(conn, question, Router())
    except Exception as e:
        log.exception("ask failed")
        return templates.TemplateResponse(request, "_ask.html", {"res": None, "error": str(e)[:300]})
    head = query.headline(res) if res["mode"] == "list" else ""
    return templates.TemplateResponse(request, "_ask.html", {"res": res, "head": head, "error": None})


@app.get("/item/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int):
    ctx = users.resolve()
    with db.user_session(ctx) as conn:
        item = threads.show_raw(conn, ctx, item_id, actor="web")
        if item is None:
            raise HTTPException(404, "not found")
        decision = triage.decision_for_item(conn, item_id)
        card = show.card(conn, item_id)            # no model call on page load: the stored summary or snippet
        try:                                   # [] before migration 015
            rows = projects.list_projects(conn)
            names = {r["id"]: r["name"] for r in rows}
            plist = [{"id": r["id"], "name": f"{names[r['parent_id']]} / {r['name']}" if r["parent_id"] in names
                      else r["name"]} for r in rows if r["status"] == "active"]
            plist.sort(key=lambda r: r["name"].lower())
        except Exception:
            plist = []
        filed = projects.links_for_item(conn, item_id)
    return templates.TemplateResponse(request, "item.html", {"item": item, "decision": decision, "page": "",
                                                             "projects": plist, "filed": filed, "card": card})


def _flow(state: str | None = None) -> Flow:
    s = settings()
    if not os.path.exists(s.google_client_file):
        raise HTTPException(500, f"Google OAuth client file not found at {s.google_client_file}. See docs/SETUP.md")
    return Flow.from_client_secrets_file(s.google_client_file, scopes=SCOPES, state=state,
                                         redirect_uri=f"{s.public_url}/oauth/google/callback")


@app.get("/oauth/google/start")
def google_start(user: str | None = None):
    ctx = users.resolve(user)
    flow = _flow()
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v[0] > 900]:
        _pending.pop(k, None)
    _pending[state] = (now, flow, ctx.email)
    return RedirectResponse(url)


@app.get("/oauth/google/callback")
def google_callback(request: Request, state: str, code: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Google returned an error: {error}")
    entry = _pending.pop(state, None)
    if entry is None:
        raise HTTPException(400, "unknown or expired sign-in attempt; start again")
    _, flow, email = entry
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # allows the http://localhost redirect during local use
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code)
    creds = json.loads(flow.credentials.to_json())
    address, _ = GmailChannel(creds).identity()
    ctx = users.resolve(email)
    s = settings()
    token_enc = crypto.encrypt(s.master_key, ctx.tenant_id, ctx.user_id, json.dumps(creds).encode())
    with db.user_session(ctx) as conn:
        account_id = store.upsert_account(conn, "gmail", address, token_enc)
        store.audit(conn, "user", "account_linked", str(account_id), {"provider": "gmail", "address": address})
    log.info("linked gmail %s for %s", address, email)
    return RedirectResponse("/?linked=" + address, status_code=303)


# ---------- Outlook.com (Microsoft Graph) ----------

def _ms_redirect() -> str:
    return f"{settings().public_url}/oauth/microsoft/callback"


@app.get("/oauth/microsoft/start")
def microsoft_start(user: str | None = None, kind: str = "personal"):
    """Authorisation code + PKCE; `state` is single-use and expires like the Google flow's.
    kind=personal: Outlook.com / Hotmail (EMAILD_MS_TENANT, 'consumers'); kind=work: a work or school Microsoft 365
    account (EMAILD_MS_WORK_TENANT, 'organizations' or one directory). The kind and authority travel in the
    server-side state entry and are stored on the account at the callback."""
    if kind not in outlook.KINDS:
        raise HTTPException(400, "kind must be personal or work")
    ctx = users.resolve(user)
    ms = outlook.MsApp.for_kind(kind)
    if not ms.client_id:
        raise HTTPException(500, "EMAILD_MS_CLIENT_ID is not set. See docs/SETUP.md (Linking an Outlook.com account)")
    state = secrets.token_urlsafe(24)
    verifier, challenge = outlook.pkce_pair()
    _prune(_ms_pending)
    _ms_pending[state] = (time.time(), verifier, ctx.email, kind, ms.tenant)
    return RedirectResponse(outlook.authorize_url(ms, _ms_redirect(), state, challenge))


def _ms_error_page(kind: str, error: str | None, description: str | None = None) -> HTMLResponse:
    """A readable failure page: the AADSTS code, what it means and what to do. Everything escaped; Microsoft's
    description is cut to its first sentence (it never carries secrets, and the client secret is blanked anyway)."""
    info = outlook.explain_error(error, description)
    what = "work or school account" if kind == "work" else "Outlook account"
    code = f" <code>{html.escape(info['code'])}</code>" if info["code"] else ""
    detail = f"<p class=\"muted\">Microsoft said: {html.escape(info['detail'])}</p>" if info["detail"] else ""
    body = (f"<!doctype html><meta charset=\"utf-8\"><title>Linking failed · emAIl</title>"
            f"<body style=\"font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px\">"
            f"<h1>Couldn't link your {what}</h1><h2>{html.escape(info['title'])}{code}</h2>"
            f"<p>{html.escape(info['help'])}</p>{detail}<p><a href=\"/\">Back to emAIl</a></p></body>")
    return HTMLResponse(body, status_code=400)


@app.get("/oauth/microsoft/callback")
def microsoft_callback(state: str, code: str | None = None, error: str | None = None,
                       error_description: str | None = None):
    entry = _ms_pending.pop(state, None)  # consume the state even on error, so it can't be replayed
    kind = entry[3] if entry and len(entry) > 3 else "personal"
    if error:
        return _ms_error_page(kind, error, error_description)
    if entry is None or time.time() - entry[0] > PENDING_SECONDS:
        raise HTTPException(400, "unknown or expired sign-in attempt; start again")
    if not code:
        raise HTTPException(400, "Microsoft did not return an authorisation code; start again")
    _, verifier, email = entry[:3]
    tenant = entry[4] if len(entry) > 4 else None
    ms = outlook.MsApp.for_kind(kind)
    if tenant:
        ms = outlook.MsApp(ms.client_id, ms.client_secret, tenant)    # exactly the authority the user signed in at
    try:
        creds = outlook.exchange_code(ms, code, verifier, _ms_redirect())
    except (outlook.ReauthRequired, outlook.TokenError) as e:
        return _ms_error_page(kind, getattr(e, "error", None) or "token_error",
                              getattr(e, "description", None) or str(e))
    address, _ = outlook.OutlookChannel(creds, app=ms).identity()
    if not address:
        raise HTTPException(400, "Microsoft did not return a mailbox address for this account")
    ctx = users.resolve(email)
    s = settings()
    token_enc = crypto.encrypt(s.master_key, ctx.tenant_id, ctx.user_id, json.dumps(creds).encode())
    # personal: "" = NULL (the EMAILD_MS_TENANT default, as for every account linked before work accounts existed);
    # work: the authority it was linked with, and the strictest privacy default (local models only)
    extra = {"ms_tenant": ms.tenant, "privacy_policy": "local_only"} if kind == "work" else {"ms_tenant": ""}
    try:
        with db.user_session(ctx) as conn:
            account_id = store.upsert_account(conn, "outlook", address, token_enc, **extra)
            store.audit(conn, "user", "account_linked", str(account_id),
                        {"provider": "outlook", "address": address, "kind": kind})
    except store.MigrationNeeded as e:
        raise HTTPException(400, str(e)) from e
    log.info("linked outlook (%s) %s for %s", kind, address, email)
    return RedirectResponse("/?linked=" + address, status_code=303)
