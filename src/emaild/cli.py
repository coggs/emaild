"""emaild command line. Run inside the container: `podman compose run --rm api <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("googleapiclient.discovery_cache", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def rules_intent(text: str) -> bool:
    from . import rules
    return rules.parse_intent(text) is not None


def print_query_result(res: dict) -> None:
    if res.get("interpreted"):
        print(f"Interpreted as: {res['interpreted']}\n")
    if res["mode"] == "list":
        from .query import headline
        items = res.get("items") or []
        if not items:
            print("Nothing matched.")
            return
        print(headline(res))
        for it in items:
            summ = it["summary"] if it["summary"] and it["summary"] != it["subject"] else ""
            print(f"  [{it['item_id']}] {it['date']}  {it['sender']}\n      {it['subject']}"
                  + (f"\n      {summ[:160]}" if summ else ""))
        return
    print(res["answer"], "\n")
    for src in res["sources"]:
        print(f"  [{src['n']}] {src['date'][:10]}  {src['from']}  —  {src['subject']}")


def print_rule(rule: dict) -> None:
    from . import rules
    kind = "guidance" if rule.get("kind") == "guidance" else "rule"
    print(f"[{rule['id']}] {rule.get('name') or ''}  ({kind}, {rules.status_text(rule)}, v{rule.get('version', 1)}, "
          f"fired {rule.get('fire_count', 0)}x" + (f", last {rule['last_fired_at']}" if rule.get("last_fired_at")
                                                   else "") + ")")
    print(f"     {rule.get('readback') or ''}")
    for w in rule.get("warnings") or []:
        print(f"     ! {w}")


def _resolve_rule(conn, ref: str | None) -> dict | None:
    from . import rules
    if not ref:
        print("give a rule id (from 'emaild rules') or a few words from it")
        return None
    r = rules.find_rule(conn, ref)
    if r is None:
        print(f"no rule matches {ref!r}")
    return r


def print_dry_run(res: dict | None, examples: int = 3) -> None:
    from . import rules
    if not res:
        return
    print(f"     {res['summary']}")
    for line in rules.example_lines(res, examples):
        print(f"       · {line}")


def _ask_confirm(conn, rule: dict, yes: bool, stdin, dry: dict | None = None) -> None:
    """Read-back (and dry run) shown; turn it on (--yes or 'y'), discard it ('n'), or leave it pending (no terminal)."""
    from . import rules
    print_rule(rule)
    print_dry_run(dry)
    if yes:
        ok = True
    elif stdin.isatty():
        ok = input("Save this rule? [y/N] ").strip().lower() in ("y", "yes")
    else:
        print(f"left pending (no terminal to ask). Turn it on with:  emaild rule confirm {rule['id']}")
        return
    if ok:
        res = rules.confirm(conn, rule["id"], actor="cli")
        re_ = res.get("reapplied") or {}
        extra = ", ".join(f"{v} {k}" for k, v in re_.items() if k != "checked" and v)
        print(f"rule {rule['id']} is on" + (f" (open decisions: {extra})" if extra else ""))
    elif rule.get("version", 1) == 1:
        rules.delete(conn, rule["id"], actor="cli")
        print("discarded")
    else:
        print(f"the new version is waiting; turn it on with:  emaild rule confirm {rule['id']}")


def run_rule(a, stdin=None) -> None:
    """`emaild rule add|edit|off|on|rm|show|confirm|apply ...` and `emaild rules` (list)."""
    from . import db, rules, users
    from .llm.router import Router
    stdin = stdin or sys.stdin
    ctx = users.resolve()
    args = list(a.args or [])
    with db.user_session(ctx) as conn:
        if a.op == "list":
            rows = rules.list_rules(conn, include_deleted=getattr(a, "all", False))
            if not rows:
                print('no rules yet - try:  emaild rule add "Always archive Strava emails"')
            for r in rows:
                print_rule(r)
            return
        if a.op == "add":
            text = " ".join(args).strip()
            if not text:
                print('give the rule in plain words:  emaild rule add "Always archive Strava emails"')
                return
            router = Router()
            rule = rules.create(conn, text, router, actor="cli")
            if rule.get("error"):
                print(rule["error"])
                return
            _ask_confirm(conn, rule, a.yes, stdin, rules.dry_run_safe(conn, rule, router))
            return
        if a.op == "apply":
            days = a.days or 14
            print(f"re-checked open decisions from the last {days} days: {rules.apply_now(conn, days)}")
            return
        if a.op == "test":
            res = rules.dry_run_ref(conn, " ".join(args), days=a.days or 30, router=Router(),
                                    sample=a.sample or rules.DRY_SAMPLE)
            if res.get("error"):
                print(res["error"])
                return
            r = res["rule"]
            print(f"[{r['id']}] {r.get('name') or ''}" if r.get("id") else f"(new, not saved) {r.get('name') or ''}")
            print(f"     {r.get('readback') or ''}")
            print_dry_run(res["dry_run"], examples=8)
            if res["dry_run"].get("truncated"):
                print(f"     (only the newest {rules.DRY_ROW_CAP} candidate emails were checked)")
            return
        if a.op == "suggest":
            if a.accept is not None:
                res = rules.accept_suggestion(conn, a.accept, actor="cli")
                if res.get("error"):
                    print(res["error"])
                    return
                re_ = (res.get("confirm") or {}).get("reapplied") or {}
                extra = ", ".join(f"{v} {k}" for k, v in re_.items() if k != "checked" and v)
                print(f"saved as rule {res['rule']['id']} and turned on" + (f" (open decisions: {extra})"
                                                                              if extra else ""))
                print(f"     {res['rule'].get('readback') or ''}")
                return
            if a.dismiss is not None:
                print("dismissed - it won't be suggested again" if rules.dismiss_suggestion(conn, a.dismiss,
                                                                                             actor="cli")
                      else f"no open suggestion {a.dismiss}")
                return
            rows = rules.list_suggestions(conn, key=ctx.user_id, refresh=True)
            if not rows:
                print("no rule suggestions right now (they come from emails you've reviewed)")
            for sg in rows:
                print(f"[{sg['id']}] {sg['label']}: “{sg['text']}”")
                print(f"     {sg['readback']}")
                print(f"     why: {sg['evidence']}")
            if rows:
                print("save one:  emaild rule suggest --accept N    never again:  emaild rule suggest --dismiss N")
            return
        r = _resolve_rule(conn, args[0] if args else None)
        if r is None:
            return
        rid = r["id"]
        if a.op == "show":
            sh = rules.show(conn, rid)
            print_rule(sh)
            print(f"     your words: {sh['original_text']}")
            for v in sh["history"]:
                print(f"     v{v['version']}  {v['created_at']}  {v['original_text']}")
        elif a.op == "confirm":
            if r["status"] != "pending":
                print(f"rule {rid} is {rules.status_text(r)}; nothing to confirm")
            else:
                _ask_confirm(conn, {**r, "version": 1}, True, stdin)
        elif a.op == "edit":
            text = " ".join(args[1:]).strip()
            if not text:
                print('give the new wording:  emaild rule edit <id> "<text>"')
                return
            router = Router()
            new = rules.edit(conn, rid, text, router, actor="cli")
            if new.get("error"):
                print(new["error"])
                return
            _ask_confirm(conn, new, a.yes, stdin, rules.dry_run_safe(conn, new, router))
        elif a.op == "off":
            until = None
            if a.until:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                from .config import settings
                until = rules.parse_until(a.until, datetime.now(ZoneInfo(settings().timezone)).date())
                if until is None:
                    print(f"can't read {a.until!r} as a date; try 2026-11-01 or February")
                    return
            ok = rules.set_enabled(conn, rid, False, until, actor="cli")
            print((f"rule {rid} is off" + (f" until {until}" if until else "")) if ok else
                  f"rule {rid} is {rules.status_text(r)}")
        elif a.op == "on":
            if r["status"] == "pending":
                _ask_confirm(conn, {**r, "version": 1}, True, stdin)
            else:
                print(f"rule {rid} is on" if rules.set_enabled(conn, rid, True, actor="cli")
                      else f"rule {rid} is {rules.status_text(r)}")
        elif a.op == "rm":
            print(f"rule {rid} deleted" if rules.delete(conn, rid, actor="cli") else "already deleted")


def print_boards(boards: list[dict], per_board: int = 25) -> None:
    from . import trackers
    if not boards:
        print('no trackers yet - try:  emaild tracker add "Track my Acme Shop orders"')
    for b in boards:
        t = b["tracker"]
        print(f"[{t['id']}] {b['icon']} {t['name']}  ({t['kind']}, {trackers.status_text(t)}, v{t['version']})")
        for it in b["open"][:per_board]:
            extra = "".join(f"  {x}" for x in (it.get("when"), f"! {it['stalled']}" if it.get("stalled") else None,
                                               f"(email {it['email_id']})" if it.get("email_id") else None) if x)
            print(f"     {it['label']:<17} {(it.get('title') or '')[:60]}{extra}")
        if not b["open"]:
            print("     (nothing on the board yet)" if t["status"] == "active" else "     (not running)")
        if b["closed"]:
            print(f"     + {len(b['closed'])} finished")


def print_tracker(t: dict, dry: dict | None = None) -> None:
    from . import trackers
    print(f"[{t['id']}] {t.get('name') or ''}  ({t.get('kind')}, {trackers.status_text(t)}, v{t.get('version', 1)})")
    print(f"     {t.get('readback') or ''}")
    for w in t.get("warnings") or []:
        print(f"     ! {w}")
    if dry:
        print(f"     {dry['summary']}")
        for e in dry.get("examples") or []:
            print(f"       · {e['title'][:70]}: {e['state'].replace('_', ' ')}")


def print_tracker_status(ans: dict) -> None:
    print(ans["title"])
    for line in ans["lines"]:
        print(f"  • {line}")
    if ans.get("hint"):
        print(f'  try:  emaild tracker add "{ans["hint"]}"')


def run_tracker(a, stdin=None) -> None:
    """`emaild tracker add|show|off|on|rm|test|edit|confirm|suggest ...` and `emaild trackers` (boards)."""
    from . import db, trackers, users
    from .llm.router import Router
    stdin = stdin or sys.stdin
    ctx = users.resolve()
    args = list(a.args or [])
    with db.user_session(ctx) as conn:
        if a.op == "list":
            print_boards(trackers.boards(conn))
            n = trackers.count_suggestions(conn)
            if n:
                print(f"\n{n} suggested tracker{'s' if n != 1 else ''}:  emaild tracker suggest")
            return
        if a.op in ("add", "edit"):
            ref = args.pop(0) if a.op == "edit" and args else None
            text = " ".join(args).strip()
            if not text:
                print('say what to track:  emaild tracker add "Track my Acme Shop orders"')
                return
            router = Router()
            if a.op == "edit":
                old = trackers.find_tracker(conn, ref)
                t = trackers.edit(conn, old["id"], text, router, actor="cli") if old else {"error": f"no tracker {ref!r}"}
            else:
                t = trackers.create(conn, text, router, actor="cli")
            if t.get("error"):
                print(t["error"])
                return
            print_tracker(t, trackers.dry_run_safe(conn, t, router))
            if a.yes:
                ok = True
            elif stdin.isatty():
                ok = input("Save this tracker? [y/N] ").strip().lower() in ("y", "yes")
            else:
                print(f"left pending (no terminal to ask). Turn it on with:  emaild tracker on {t['id']}")
                return
            if ok:
                trackers.confirm(conn, t["id"], actor="cli")
                print(f"tracker {t['id']} is on; its board fills in over the next worker cycles")
            elif t.get("version", 1) == 1:
                trackers.delete(conn, t["id"], actor="cli")
                print("discarded")
            return
        if a.op == "test":
            res = trackers.dry_run_ref(conn, " ".join(args), router=Router(), days=a.days or trackers.DRY_DAYS)
            if res.get("error"):
                print(res["error"])
                return
            t = res["tracker"]
            print(f"[{t['id']}] {t.get('name') or ''}" if t.get("id") else f"(new, not saved) {t.get('name') or ''}")
            print(f"     {t.get('readback') or ''}")
            print(f"     {res['dry_run']['summary']}")
            return
        if a.op == "suggest":
            if a.accept is not None:
                res = trackers.accept_suggestion(conn, a.accept, actor="cli")
                print(res["error"] if res.get("error") else f"tracking as {res['tracker']['id']}: "
                                                              f"{res['tracker'].get('readback') or ''}")
                return
            if a.dismiss is not None:
                print("dismissed - it won't be suggested again" if trackers.dismiss_suggestion(conn, a.dismiss,
                                                                                                actor="cli")
                      else f"no open suggestion {a.dismiss}")
                return
            rows = trackers.list_suggestions(conn, key=ctx.user_id)
            if not rows:
                print("no tracker suggestions right now")
            for sg in rows:
                print(f"[{sg['id']}] {sg['label']}: “{sg['text']}”\n     {sg['readback']}\n     why: {sg['evidence']}")
            if rows:
                print("track one:  emaild tracker suggest --accept N    never again:  emaild tracker suggest --dismiss N")
            return
        t = trackers.find_tracker(conn, " ".join(args)) if args else None
        if t is None:
            print(f"no tracker matches {' '.join(args)!r} (see 'emaild trackers')" if args else
                  "give a tracker id (from 'emaild trackers') or a few words from it")
            return
        tid = t["id"]
        if a.op == "show":
            b = trackers.board(conn, tid)
            print_boards([b] if b else [], per_board=200)
            print(f"     {t['readback']}\n     your words: {t['original_text']}")
            for it in (b or {}).get("closed") or []:
                print(f"     finished {it['closed_at'][:10]}  {it['label']:<14} {it.get('title') or ''}")
        elif a.op in ("on", "confirm"):
            ok = trackers.confirm(conn, tid, actor="cli")["active"] if t["status"] == "pending" else \
                trackers.set_enabled(conn, tid, True, actor="cli")
            print(f"tracker {tid} is on" if ok else f"tracker {tid} is {trackers.status_text(t)}")
        elif a.op == "off":
            print(f"tracker {tid} is paused" if trackers.set_enabled(conn, tid, False, actor="cli")
                  else f"tracker {tid} is {trackers.status_text(t)}")
        elif a.op == "rm":
            print(f"tracker {tid} deleted" if trackers.delete(conn, tid, actor="cli") else "already deleted")


def print_projects(rows: list[dict]) -> None:
    from . import projects
    if not rows:
        print('no projects yet - try:  emaild project add "Create a project for the NSFC committee, everything from '
              'nsfc.example.org"')
    for top in rows:
        icon = "🗂" if top["kind"] == "umbrella" else "📁"
        print(f"[{top['id']}] {icon} {top['name']}  ({top['kind']}, {projects.status_text(top)})  {top['line']}")
        for k in top["children"]:
            print(f"     [{k['id']}] 📁 {k['name']}  ({projects.status_text(k)})  {k['line']}")


def print_project(p: dict, dry: dict | None = None) -> None:
    from . import projects
    under = f", under {p['parent_name']}" if p.get("parent_name") else ""
    print(f"[{p['id']}] {p.get('name') or ''}  ({p.get('kind')}{under}, {projects.status_text(p)})")
    print(f"     {p.get('readback') or ''}")
    for w in p.get("warnings") or []:
        print(f"     ! {w}")
    if dry and dry.get("summary"):
        print(f"     {dry['summary']}")
        for e in (dry.get("examples") or [])[:3]:
            print(f"       · {e['date']}  {e['sender']} — {e['subject']}")


def run_project(a, stdin=None) -> None:
    """`emaild project add|show|status|link|unlink|done|archive|on|rm|move|confirm|facts|suggest ...` and
    `emaild projects`."""
    from . import db, projects, users
    from .llm.router import Router
    stdin = stdin or sys.stdin
    ctx = users.resolve()
    args = list(a.args or [])
    with db.user_session(ctx) as conn:
        if a.op == "list":
            print_projects(projects.overview_rows(conn))
            sugg = projects.list_suggestions(conn)
            if sugg:
                print(f"\n{len(sugg)} suggested sub-project{'s' if len(sugg) != 1 else ''}:  emaild project suggest")
            return
        if a.op == "add":
            text = " ".join(args).strip()
            if not text and a.item is None:
                print('say what the project is:  emaild project add "Add a sub-project under NSFC: presentation night"')
                return
            router = Router()
            p = projects.create(conn, text, router, actor="cli", parent=a.under, item_id=a.item)
            if p.get("error"):
                print(p["error"])
                return
            print_project(p, projects.dry_run_safe(conn, p, router))
            if a.yes:
                ok = True
            elif stdin.isatty():
                ok = input("Save this project? [y/N] ").strip().lower() in ("y", "yes")
            else:
                print(f"left pending (no terminal to ask). Save it with:  emaild project confirm {p['id']}")
                return
            if ok:
                res = projects.confirm(conn, p["id"], actor="cli")
                extra = f"; {res['linked']} emails from the thread filed" if res.get("linked") else ""
                print(f"project {p['id']} is on{extra}; matching emails from the last {projects.WINDOW_DAYS} days are "
                      f"filed over the next worker cycles")
            else:
                projects.delete(conn, p["id"], actor="cli")
                print("discarded")
            return
        if a.op == "suggest":
            if a.accept is not None:
                res = projects.accept_suggestion(conn, a.accept, actor="cli")
                print(res["error"] if res.get("error") else f"created sub-project {res['project']['id']}: "
                                                              f"{res['project']['name']}")
                return
            if a.dismiss is not None:
                print("dismissed - it won't be suggested again" if projects.dismiss_suggestion(conn, a.dismiss, "cli")
                      else f"no open suggestion {a.dismiss}")
                return
            rows = projects.list_suggestions(conn)
            for sg in rows:
                print(f"[{sg['id']}] {sg['parent']} / {sg['name']}  — {sg['evidence']}")
            print("create one:  emaild project suggest --accept N    never again:  emaild project suggest --dismiss N"
                  if rows else "no suggested sub-projects right now")
            return
        if a.op in ("link", "unlink"):
            if len(args) < 2 or not args[0].isdigit():
                print(f"use:  emaild project {a.op} <email id> <project>")
                return
            if a.op == "link":
                res = projects.link(conn, int(args[0]), " ".join(args[1:]), actor="cli")
                print(res["error"] if res.get("error") else
                      f"email {res['item_id']} filed under {res['project']}" + ("" if res["linked"] else
                                                                                " (it already was)"))
            else:
                print("unlinked" if projects.unlink(conn, int(args[0]), " ".join(args[1:]), actor="cli")
                      else "it wasn't filed there")
            return
        p = projects.find_project(conn, " ".join(args)) if args else None
        if p is None:
            print(f"no project matches {' '.join(args)!r} (see 'emaild projects')" if args else
                  "give a project id (from 'emaild projects') or its name")
            return
        pid = p["id"]
        if a.op in ("status", "show"):
            st = projects.project_status(conn, p, Router() if a.op == "status" else None,
                                         with_overview=a.op == "status")
            for line in projects.status_lines(st):
                print(line)
            if a.op == "show":
                print(f"     {p['readback']}\n     your words: {p['original_text']}")
                for e in st["timeline"][:10]:
                    print(f"     {e['at'][:16]}  {e['kind']:<8} {e['text'][:90]}")
        elif a.op == "facts":
            res = projects.facts(conn, pid, a.type, a.status)
            for f in res["facts"]:
                due = f"  due {f['due']}" if f["due"] else ""
                print(f"  [{f['id']}] {f['type']:<13} {f['status']:<10} {f['project'][:20]:<20} {f['text'][:80]}{due}"
                      f"  (email {f['item_id']})")
            if not res["facts"]:
                print("no facts")
        elif a.op == "confirm":
            res = projects.confirm(conn, pid, actor="cli")
            print(f"project {pid} is on" if res["active"] else f"project {pid} is {projects.status_text(p)}")
        elif a.op in ("done", "archive", "on"):
            st = {"done": "done", "archive": "archived", "on": "active"}[a.op]
            print(f"project {pid} is {st}" if projects.set_status(conn, pid, st, actor="cli")
                  else f"project {pid} is {projects.status_text(p)}")
        elif a.op == "rm":
            print(f"project {pid} deleted" if projects.delete(conn, pid, actor="cli") else "already deleted")
        elif a.op == "move":
            if a.under is None and not a.top:
                print("say where:  emaild project move <project> --under <parent>   (or --top)")
                return
            res = projects.move(conn, pid, None if a.top else a.under, actor="cli")
            print(res["error"] if res.get("error") else
                  f"project {pid} is now " + (f"under {res['parent_name']}" if res.get("parent_name") else "top-level"))


def run_thread_status(a) -> None:
    from . import db, projects, users
    from .llm.router import Router
    ref = " ".join(a.ref or []).strip()
    with db.user_session(users.resolve()) as conn:
        ts = projects.thread_status(conn, int(ref) if ref.isdigit() else None, None if ref.isdigit() else ref,
                                    Router())
    if ts.get("error"):
        print(ts["error"])
        return
    for line in projects.thread_lines(ts):
        print(line)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="emaild")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create DB users, apply migrations, create the first tenant/user")
    sub.add_parser("migrate", help="apply pending migrations")
    lm = sub.add_parser("load-model", help="load the ONNX embedding model from models/ into the DB")
    lm.add_argument("--file")
    lm.add_argument("--name")
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", default="")
    cu.add_argument("--tenant", help="tenant name (defaults to EMAILD_TENANT_NAME)")
    cu.add_argument("--role", default="member", choices=["owner", "admin", "member"])
    sub.add_parser("sync-once", help="run one sync + embed cycle for all accounts")
    sub.add_parser("embed-pending", help="embed all pending chunks")
    sub.add_parser("worker", help="run the sync loop forever")
    sub.add_parser("api", help="run the web app on :8080")
    m = sub.add_parser("mcp", help="run the MCP server")
    m.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    ap = sub.add_parser("ask", help='natural-language question, e.g. "last 5 emails from Matt" or '
                                     '"what are the latest perks from JB Hi-Fi?"')
    ap.add_argument("question")
    ap.add_argument("--raw", action="store_true", help="skip query understanding (search on the whole sentence)")
    sub.add_parser("status")
    sub.add_parser("verify", help="compare the provider's message list for the backfill window with what is stored")
    sub.add_parser("rescan", help="re-run the backfill for all accounts (fetches only missing messages)")
    sub.add_parser("check-llm", help="verify the LLM endpoint and model are reachable")
    br = sub.add_parser("brief", help="generate a brief now and print it")
    br.add_argument("--hours", type=int, help="look back this many hours (default: since the last morning brief)")
    sub.add_parser("telegram", help="run the Telegram bot (brief scheduler, alerts, review, questions)")
    sub.add_parser("telegram-code", help="print a one-time code to link a Telegram chat")
    tr = sub.add_parser("triage", help="triage pending emails now (shadow mode)")
    tr.add_argument("--limit", type=int, default=25)
    sub.add_parser("cold-start", help="rebuild sender stats from your sent mail and show top contacts")
    sub.add_parser("triage-stats")
    sub.add_parser("refresh", help="re-check open decisions against today's rules (spam, impersonation, one-time codes)")
    ex = sub.add_parser("explain", help="show emAIl's verdict for emails whose subject contains these words")
    ex.add_argument("subject")
    sub.add_parser("security-sweep", help="(same as refresh)")
    sub.add_parser("needs", help="list what's in Needs attention (with ids for 'emaild seen')")
    sn = sub.add_parser("seen", help="clear emails from Needs attention: 'emaild seen 12 15' or 'emaild seen --all'")
    sn.add_argument("ids", nargs="*", type=int)
    sn.add_argument("--all", action="store_true")
    sub.add_parser("unsubs", help="list unsubscribe suggestions (list mail you never engage with)")
    us = sub.add_parser("unsub", help="unsubscribe from a sender's list (one-click), or --dismiss to keep it")
    us.add_argument("sender_addr")
    us.add_argument("--dismiss", action="store_true", help="keep getting these; never suggest again")
    fu = sub.add_parser("followups", help="emails you sent that are still waiting on a reply")
    fu.add_argument("--dismiss", type=int, metavar="ITEM_ID", help="stop reminding me about this one")
    pr = sub.add_parser("protect", help="protect a person/organisation name against impersonation")
    pr.add_argument("name", nargs="?", help='e.g. "Alex Rivera" (omit to list)')
    pr.add_argument("--allow", action="append", default=[], help="allowed address or domain (repeatable)")
    pr.add_argument("--org", action="store_true", help="organisation: match the name anywhere in the display name")
    pr.add_argument("--note")
    pr.add_argument("--remove", action="store_true")
    pr.add_argument("--suggest", action="store_true", help="show role-name senders and club domains found in your mail")
    ru = sub.add_parser("rule", help='rules in plain words: add "<text>" | edit <id> "<text>" | off <id> [--until DATE] '
                                     '| on <id> | rm <id> | show <id> | confirm <id> | apply | test <id|words|"new '
                                     'rule"> [--days N] | suggest [--accept N | --dismiss N]')
    ru.add_argument("op", choices=["add", "edit", "off", "on", "rm", "show", "confirm", "apply", "list", "test",
                                   "suggest"])
    ru.add_argument("args", nargs="*", help="rule text, or a rule id (or a few words from it) then text")
    ru.add_argument("--yes", action="store_true", help="save without asking")
    ru.add_argument("--until", help="with off: until this date, e.g. 2026-11-01 or February")
    ru.add_argument("--days", type=int, default=None,
                    help="with apply: re-check open decisions from this many days (default 14); with test: dry-run "
                         "over this many days (default 30)")
    ru.add_argument("--sample", type=int, default=None,
                    help="with test: emails the model checks for a rule with a condition (default 8, max 20)")
    ru.add_argument("--accept", type=int, help="with suggest: save suggestion N as a rule (and turn it on)")
    ru.add_argument("--dismiss", type=int, help="with suggest: never suggest N again")
    rs = sub.add_parser("rules", help="list your rules")
    rs.add_argument("--all", action="store_true", help="include deleted rules")
    tk = sub.add_parser("tracker", help='trackers in plain words: add "<text>" [--yes] | show <id|words> | off <id> | '
                                         'on <id> | rm <id> | test <id|"new tracker"> | edit <id> "<text>" | '
                                         'suggest [--accept N | --dismiss N]')
    tk.add_argument("op", choices=["add", "show", "off", "on", "rm", "test", "edit", "confirm", "suggest", "list"])
    tk.add_argument("args", nargs="*", help="tracker text, or a tracker id (or a few words from it)")
    tk.add_argument("--yes", action="store_true", help="save without asking")
    tk.add_argument("--days", type=int, default=None, help="with test: look back this many days (default 90)")
    tk.add_argument("--accept", type=int, help="with suggest: track suggestion N")
    tk.add_argument("--dismiss", type=int, help="with suggest: never suggest N again")
    sub.add_parser("trackers", help="your trackers' boards")
    pj = sub.add_parser("project", help='projects in plain words: add "<text>" [--yes] [--under P] [--item N] | '
                                        'show|status <project> | link|unlink <email id> <project> | '
                                        'done|archive|on|rm <project> | move <project> --under <parent> | '
                                        'confirm <project> | facts <project> | suggest [--accept N | --dismiss N]')
    pj.add_argument("op", choices=["add", "show", "status", "link", "unlink", "done", "archive", "on", "rm", "move",
                                   "confirm", "facts", "suggest", "list"])
    pj.add_argument("args", nargs="*", help="project text, or a project id / name")
    pj.add_argument("--yes", action="store_true", help="save without asking")
    pj.add_argument("--under", default=None, help="parent project (add, move)")
    pj.add_argument("--top", action="store_true", help="with move: make it top-level")
    pj.add_argument("--item", type=int, default=None, help="with add: start it from this email's thread")
    pj.add_argument("--type", default=None, help="with facts: decision|ask|commitment|deadline|open_question|info")
    pj.add_argument("--status", default="open", help="with facts: open|done|superseded|all")
    pj.add_argument("--accept", type=int, help="with suggest: create suggested sub-project N")
    pj.add_argument("--dismiss", type=int, help="with suggest: never suggest N again")
    sub.add_parser("projects", help="your projects and sub-projects")
    ts = sub.add_parser("thread-status", help='where a thread stands: thread-status <email id | "words">')
    ts.add_argument("ref", nargs="+")
    bm = sub.add_parser("benchmark", help="re-classify reviewed emails and score agreement with your verdicts")
    bm.add_argument("--model", help="Ollama model to test (default: EMAILD_TRIAGE_MODEL or EMAILD_LLM_MODEL)")
    bm.add_argument("--limit", type=int, default=50, help="most recent reviewed emails to test")
    bm.add_argument("--pipeline", action="store_true", help="score emAIl as it runs (bulk rules first), not the model alone")

    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    from .config import settings
    s = settings()

    if a.cmd == "init-db":
        from . import migrate
        migrate.bootstrap()
        print("applied:", migrate.migrate() or "nothing new")
        if s.default_user:
            u = migrate.ensure_tenant_user(s.tenant_name, s.default_user, s.default_user_name, "owner")
            print(f"user {u.email}: tenant {u.tenant_id}, user {u.user_id}")
    elif a.cmd == "migrate":
        from . import migrate
        print("applied:", migrate.migrate() or "nothing new")
    elif a.cmd == "load-model":
        from . import migrate
        migrate.load_embedding_model(a.file, a.name.upper() if a.name else None)
        print("embedding model loaded and active")
    elif a.cmd == "create-user":
        from . import migrate
        u = migrate.ensure_tenant_user(a.tenant or s.tenant_name, a.email, a.name, a.role)
        print(f"user {u.email}: tenant {u.tenant_id}, user {u.user_id}")
    elif a.cmd == "sync-once":
        from . import sync
        sync.run_once()
    elif a.cmd == "embed-pending":
        from . import sync
        for ctx in sync.users_with_accounts():
            print(ctx.email, sync.embed_user(ctx, max_batches=10_000))
    elif a.cmd == "worker":
        from . import sync
        sync.run_forever()
    elif a.cmd == "api":
        import uvicorn
        uvicorn.run("emaild.web.app:app", host=s.bind_host, port=s.api_port, log_level="info")
    elif a.cmd == "mcp":
        from . import mcp_server
        mcp_server.run(a.transport)
    elif a.cmd in ("search", "ask", "status"):
        from . import db, store, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            if a.cmd == "search":
                from .search import search
                for h in search(conn, a.query, limit=a.limit):
                    print(f"[{h.item_id}] {h.received_at[:16]}  {h.sender}\n      {h.subject}\n      {h.snippet[:160]!r}")
            elif a.cmd == "ask":
                from .llm.router import Router
                if a.raw:
                    from .ask import ask
                    res = {"mode": "answer", **ask(conn, a.question, Router())}
                else:
                    from . import query, trackers
                    intent = trackers.parse_intent(a.question)
                    if intent and intent["op"] == "status":
                        ans = trackers.status_answer(conn, intent["kind"])
                        if not ans.get("hint"):       # tracking that kind: answer from the boards
                            print_tracker_status(ans)
                            return
                    elif intent and intent["op"] == "list":
                        print_boards(trackers.boards(conn))
                        return
                    elif intent:
                        print(f'that reads as a new tracker; add it with:  emaild tracker add "{intent["text"]}"')
                        return
                    from . import projects
                    pi = None if rules_intent(a.question) else projects.parse_intent(a.question)
                    res = projects.route_status(conn, pi["ref"], Router()) if pi and pi["op"] == "status" else {}
                    if res.get("unavailable"):
                        pi = None                         # before migration 015: a normal question, as before
                    if pi and pi["op"] == "status":
                        if res.get("error"):
                            print(res["error"])
                        else:
                            lines = (projects.status_lines(res["status"]) if res["kind"] == "project"
                                     else projects.thread_lines(res["status"]))
                            print("\n".join(lines))
                        return
                    if pi and pi["op"] == "list":
                        print_projects(projects.overview_rows(conn))
                        return
                    if pi:
                        print(f'that reads as a new project; add it with:  emaild project add "{pi["text"]}"')
                        return
                    res = query.run(conn, a.question, Router())
                print_query_result(res)
            else:
                print(json.dumps(store.status(conn), indent=2, default=str))
    elif a.cmd == "telegram":
        from . import telegram
        telegram.run()
    elif a.cmd in ("brief", "telegram-code"):
        from . import brief, db, telegram, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            if a.cmd == "brief":
                b = brief.generate(conn, ctx, kind="on_demand", hours=a.hours, delivered_via="cli")
                import re as _re
                print(_re.sub(r"<[^>]+>", "", brief.render_telegram(b)))
            else:
                print(f"Send this to your bot within 10 minutes:  /link {telegram.create_code(conn)}")
    elif a.cmd == "protect":
        from . import db, identities, triage, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            if a.suggest:
                cur = conn.cursor()
                cur.execute("SELECT address FROM accounts")
                sug = identities.suggest(conn, [r[0] for r in cur])
                print("Senders using committee-role names (display name -> domain it really came from):")
                for r in sug["role_senders"]:
                    flag = "  <- personal mailbox" if r["personal_mailbox"] else ""
                    print(f"  {r['count']:>4}  {r['display_name'][:45]:<45} {r['domain']}{flag}")
                print("\nOrganisation domains your mail is addressed to:")
                for r in sug["addressed_to_domains"]:
                    print(f"  {r['count']:>4}  {r['domain']}")
                return
            if a.name and a.remove:
                print("removed" if identities.remove(conn, a.name) else "not found")
            elif a.name:
                print(identities.upsert(conn, a.name, a.allow, "org" if a.org else "person", a.note))
            for i in identities.list_all(conn):
                print(f"  {i['kind']:<6} {i['name']:<30} {', '.join(i['allowed'])}")
        if a.name and not a.remove:
            print("re-checking the last 30 days:", triage.refresh(ctx, days=30))
    elif a.cmd == "explain":
        from . import db, triage, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            rows = triage.explain_by_subject(conn, a.subject)
        if not rows:
            print("no email with that in the subject")
        for r in rows:
            d = r["decision"] or {}
            print(f"[{r['item_id']}] {r['received']}  {r['subject']}\n     from: {r['from']}")
            if d:
                print(f"     verdict: {d['action']} · {d['importance']} · {d['category']}  ({d['source']}, "
                      f"{d['status']}, confidence {d['confidence']:.2f})")
                print(f"     why: {d['reasons']}")
                for rule in r.get("rules") or []:
                    print(f"     rule: {rule['name']} (#{rule['id']}, {rule['status']}) - {rule['readback']}")
            else:
                print("     verdict: not triaged yet")
            h = r["sender_history"]
            print(f"     sender history: received {h.get('received', 0)}, you replied {h.get('replied', 0)}, "
                  f"you wrote to them {h.get('sent_to', 0)}\n")
    elif a.cmd == "rule":
        run_rule(a)
    elif a.cmd == "rules":
        a.op, a.args, a.yes, a.days = "list", [], False, None
        run_rule(a)
    elif a.cmd == "tracker":
        run_tracker(a)
    elif a.cmd == "trackers":
        a.op, a.args, a.yes, a.days, a.accept, a.dismiss = "list", [], False, None, None, None
        run_tracker(a)
    elif a.cmd == "project":
        run_project(a)
    elif a.cmd == "projects":
        a.op, a.args = "list", []
        run_project(a)
    elif a.cmd == "thread-status":
        run_thread_status(a)
    elif a.cmd in ("needs", "seen"):
        from . import brief as brief_mod, db, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            if a.cmd == "seen":
                if not a.ids and not a.all:
                    print("give one or more ids (from 'emaild needs'), or --all")
                    return
                print(f"cleared {brief_mod.dismiss(conn, None if a.all else a.ids)}")
            ny = brief_mod.needs_you(conn, days=3, limit=50)
        for title, rows in (("Alerts", ny["alerts"]), ("Waiting on your reply", ny["awaiting_reply"])):
            if rows:
                print(f"{title}:")
                for r in rows:
                    print(f"  [{r['decision_id']}] {r['received_at']}  {r['sender'][:30]:<30} {r['subject'][:60]}")
        if not (ny["alerts"] or ny["awaiting_reply"]):
            print("Nothing needs you.")
    elif a.cmd in ("unsubs", "unsub", "followups"):
        from . import db, recommend, users
        ctx = users.resolve()
        with db.user_session(ctx) as conn:
            if a.cmd == "unsubs":
                rows = recommend.suggestions(conn)
                for n, r in enumerate(rows, 1):
                    how = {"one_click": "one-click", "url": "link (you open it)", "mailto": "email (you send it)"}
                    print(f"{n:>3}. {r['sender_name'][:35]:<35} {r['sender_addr']}\n       {r['reason']}; "
                          f"last {r['last_received']}; {how[r['method']]}")
                print("\nemaild unsub <sender_addr>   ·   emaild unsub --dismiss <sender_addr>" if rows
                      else "No unsubscribe suggestions.")
            elif a.cmd == "unsub":
                res = recommend.act(conn, a.sender_addr, "dismiss" if a.dismiss else "unsubscribe", actor="cli")
                print(f"{res['status']}: {res['detail']}" + (f"\n  {res['link']}" if res.get("link") else ""))
            else:
                if a.dismiss:
                    print("dismissed" if recommend.dismiss_nudge(conn, a.dismiss) else "not found (or not sent by you)")
                rows = recommend.followup_nudges(conn, limit=50)
                for r in rows:
                    print(f"  [{r['item_id']}] {r['sent_at']}  {r['days_waiting']:>2}d  {r['to'][:30]:<30} {r['subject'][:60]}")
                if not rows:
                    print("Nobody owes you a reply.")
    elif a.cmd in ("refresh", "security-sweep"):
        from . import sync, triage
        for ctx in sync.users_with_accounts():
            print(ctx.email, triage.refresh(ctx))
    elif a.cmd in ("triage", "cold-start", "triage-stats", "benchmark"):
        from . import db, senders, sync, triage, users
        ctx = users.resolve()
        if a.cmd == "triage":
            sync.refresh_senders(ctx)
            print(json.dumps(triage.triage_user(ctx, limit=a.limit)))
        elif a.cmd == "cold-start":
            print("senders:", sync.refresh_senders(ctx, force=True))
            with db.user_session(ctx) as conn:
                for c in senders.top(conn, 15):
                    print(f"  {c['sender']:<45} replied {c['replied']:>3}  wrote to {c['sent_to']:>3}  received {c['received']:>4}")
        elif a.cmd == "triage-stats":
            with db.user_session(ctx) as conn:
                print(json.dumps(triage.stats(conn), indent=2))
        else:
            print(json.dumps(triage.benchmark(ctx, a.model, a.limit, a.pipeline), indent=2))
    elif a.cmd in ("verify", "rescan"):
        from . import sync
        for ctx, account_id in sync.active_accounts():
            if a.cmd == "verify":
                print(json.dumps(sync.verify_account(ctx, account_id), indent=2))
            else:
                sync.rescan_account(ctx, account_id)
                print(f"account {account_id}: backfill will re-run on the worker's next cycle")
    elif a.cmd == "check-llm":
        from .llm.providers import OllamaProvider
        prov = OllamaProvider(s.ollama_url, s.llm_model, s.llm_num_ctx)
        available = prov.models()
        print("ollama models:", ", ".join(available) or "(none)")
        if not any(m == s.llm_model or m.split(":")[0] == s.llm_model for m in available):
            print(f"WARNING: EMAILD_LLM_MODEL={s.llm_model} not found; set it to one of the above")
            sys.exit(1)
        r = prov.chat([{"role": "user", "content": "Reply with the single word: ready"}])
        print(f"{s.llm_model} replied {r.text.strip()!r} in {r.latency_ms} ms")


if __name__ == "__main__":
    main()
