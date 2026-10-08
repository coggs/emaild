# emAIl — Feature list

This is the backlog of features beyond the phased roadmap in the design spec. Each entry says what the feature is, how it would work, and roughly when it fits.

| # | Feature | Status | Fits after |
|---|---|---|---|
| F1 | Service status monitoring | Built (2026-10-07) as the `service` tracker kind in F5 | Phase 2 (rules) |
| F2 | Delete expired one-time codes / sign-in links in Gmail | Proposed (2026-10-07) | Phase 4 (write access) |
| F3 | Protected identities: web page + suggestions | Proposed (2026-10-07) | Phase 2 |
| F4 | One command surface: CLI, MCP and Telegram parity | In progress (2026-10-07) | ongoing |
| F5 | Trackers: status boards configured in plain language (orders, services, ticket releases…) | Built (2026-10-07): orders, service, on-sale and custom kinds on every surface | Phase 2 (rules) |
| F6 | Generic IMAP connector (app passwords: iCloud, Fastmail, ISP mailboxes) | Possible later addition | after Phase 5 |
| F7 | Draft emails (never sent): emAIl writes, you send from your own mail app | Proposed (2026-10-07) | Phase 6 (drafts) |
| F8 | Deterministic-first triage: a fast, explainable classifier ahead of the LLM (plus an honest benchmark) | Proposed (2026-10-07), not started | before Phase 4 (earned autonomy) |

**Phase 3, slice 1 — Projects core with sub-projects (2026-10-07): built.** `src/emaild/projects.py`, migration 015, setup in [SETUP.md §14](SETUP.md#14-projects-phase-3-slice-1). What shipped:
- **Hierarchy.** Every project has at most one parent. *Umbrellas* are ongoing involvements that never finish (a club committee, the household), usually with a broad match (everyone at a domain); *sub-projects* are goals with an end ("Presentation night", "Uniform order") inside the umbrella's mail. Depth > 2 is allowed in the data; the UI shows two levels. `project_related` links the rare cross-umbrella relationship (never a second parent). Deleting a project moves its sub-projects up a level.
- **Filing, two steps, after triage and trackers** (spam, phishing, one-time codes, security-held mail and duplicates are never filed or read). (1) Deterministic: thread stickiness (a thread already filed keeps new messages, including your replies, without a model call), a rule's `project:` action (strongest signal; with a topic Gemma checks the topic first), a sub-project's own match, or the umbrella's match (the rules matcher, reused). (2) Gemma picks which of *that* umbrella's open sub-projects the email is about — a closed set: an id, "none" (it stays as the umbrella's general business) or "new" (stored as a suggested sub-project, never created automatically). Each email is considered once per umbrella (`project_processed`).
- **Facts.** One narrow, schema-bound call per filed email: decision, ask (of you), commitment (with owner), deadline (needs a date), open question, info — each citing its email, with owner, due date and confidence, validated in Python. Near-identical facts within a project are dropped; a later email can resolve an open ask / question / commitment / deadline (the model picks from a short list of ids). Facts belong to the sub-project and roll up into the umbrella.
- **Bounded.** Deterministic linking is unbounded across cycles; model calls (sub-project choice, rule topics, facts) share 30 per user per worker cycle, with a third kept for facts while some wait. A new project files the last 90 days in the background; facts from emails older than 48 hours are *backfill* and never show as new in the brief.
- **Status on demand.** Built from facts, the timeline and thread state (who wrote last → who's waiting on whom, for conversations you took part in). Umbrella: a one-liner per sub-project, general business, upcoming dates across all of them. Sub-project: open facts by type. An optional 2–3 sentence overview is written by Gemma **from the facts only**. `thread_status` does the same for any thread (in a project or not): long threads are read in stages (≤ 5 calls), every entry cites its email.
- **Creating in plain words** (compile → read-back → 90-day dry run → Save, like rules and trackers): from a description, from a sender/domain, as a sub-project, or from a thread ("make this thread a sub-project of NSFC" via the item page, MCP `item_id` or CLI `--item`). Deterministic parser for the common shapes.
- **Rules integration.** Rules gain an optional `project:` action ("anything from Riverside Rovers about the canteen goes under the club's Canteen sub-project"), validated, read back and resolved to the project's id at compile time. Rules without it compile exactly as before; a rule whose only effect is filing never changes triage.
- **Next (slice 2):** the linked Obsidian note per project (`obsidian_path` is reserved).

**Phase 5, Outlook.com (2026-10-07): delivered** through Microsoft Graph (`src/emaild/channels/outlook.py`). Read-only `Mail.Read` over OAuth (auth code + PKCE, `consumers` authority). It uses per-folder delta sync for Inbox, Sent, Junk and Archive, and fetches raw MIME through the same parser as Gmail. Outlook state is mapped onto the Gmail label names, so triage, search and briefs work unchanged. Setup is in `docs/SETUP.md` (5b). **Work or school accounts (Microsoft 365 / Entra ID) are supported too (2026-10-09):** a second link flow uses `EMAILD_MS_WORK_TENANT` (`organizations`, or one directory's tenant ID or domain), each account stores the authority it was linked with (migration 016) so refreshes go there, Entra errors (admin consent, Conditional Access, wrong account type) are explained with their AADSTS code, and a new work account defaults to `local_only`. A generic IMAP connector (F6) could later cover providers that still allow app passwords (iCloud, Fastmail, many ISPs). Outlook.com can't use it because Microsoft turned off basic auth there in September 2024.

---

## F1. Service status monitoring

*Built as part of [F5](#f5-trackers-status-boards-configured-in-plain-language) (2026-10-07): service status is the `service` tracker kind. "Track Example VPN status" gives a board per service/component with alerts on change only, a "recovered" note, last-heard times and optional silence warnings ("it reports daily"). The design below is kept for reference; the data lives in the shared tracker tables rather than `service_sources` / `service_events`, and the MCP tools are `trackers()` / `tracker_items()` rather than `service_status()`.*

**What:** some services send regular emails about their own state. Twingate is the first example: connector up or down, an update available. emAIl should turn those emails into a **live status board** rather than an inbox. You see the *current state* of each service, and you're only told when it changes.

**How it would work**

1. **Registering a source.** Defined in plain language, like any other rule: *"Track Twingate status from emails from @twingate.com"*. emAIl can also suggest sources itself, when it notices recurring, templated emails from the same sender.
2. **Extraction.** Each matching email goes through a narrow, schema-bound call, so Gemma handles it easily: `{service, component, state: up|down|degraded|update_available|maintenance, detail, occurred_at}`. Fixed templates can skip the model entirely and use a pattern rule instead.
3. **State.** emAIl keeps the current state of each service and component, plus a history of transitions. The email it came from is cited, as usual.
4. **Noise handling.**
   - Once the state is recorded, the email itself is archived. This is an action, so it goes through earned autonomy.
   - "Still up" emails only update a last-heard timestamp.
5. **Alerts on change only.** Up→down and new-update-available go to Telegram. Down→up sends a "recovered" note. Repeats of the same state stay silent.
6. **Silence detection.** Optionally, for services that send regular heartbeat emails: *"Twingate hasn't reported in 3 days, though it usually does daily."*
7. **Where it shows up:**
   - a **Services** tile on the dashboard: green, amber or red per service, with when it last changed
   - status in the morning brief
   - an MCP tool, `service_status(service?)`, so you can ask "is everything up?" or "what needs updating?"

**Data:** two new tables, `service_sources` (rule, service name, expected cadence) and `service_events` (state transitions). Both are scoped per user like everything else.

**Later:** the same mechanism can cover other machine-generated mail. Examples: backup job reports, certificate expiry warnings, NAS/Synology alerts, pfSense notifications, CI build results, and monthly bills (state = due/paid). It's also a natural first consumer for non-email channels, such as a status bot in an instant-messaging app.


---

## F2. Delete expired codes in Gmail

emAIl already detects one-time codes and sign-in links, flags them on arrival, hides them once they expire, and scrubs its own copy. With Gmail write access (the `gmail.modify` scope, in Phase 4 under earned autonomy), it would also move them to Trash in Gmail after expiry. They're credentials with no further use, so there's no reason to keep them in the mailbox.

## F3. Protected identities: web page and suggestions

Protected identities can currently be set through the CLI and MCP. A name, such as a person ("Alex Rivera") or an organisation ("Riverside Rovers", "NHFC"), may only send from listed addresses or domains; anything else is flagged as impersonation. Next steps:
- a page in the web app to manage them
- suggestions, e.g. *"you've received 4 emails from 'Alex Rivera' at 3 different gmail addresses; protect this name?"*
- automatic protection for club and committee roles found in signatures (president, treasurer, secretary)


## F4. One command surface: CLI, MCP and Telegram

Every operation should be available wherever you are: in a terminal, in an MCP client, and from your phone. There should be no need to remember `podman compose run`.

**Done (2026-10-07)**
- An `emaild` host command (installed with `scripts/install-cli.sh`). It runs app commands inside the running container and includes `up`, `down`, `ps`, `logs` and `redeploy`.
- Impersonation protection works everywhere: the CLI `protect` / `protect --suggest`, MCP `protect_identity` / `protected_identities` / `suggest_protection`, and Telegram `/protect`, `/protectorg`, `/unprotect`, `/protected` and `/suggest`.

**Parity today**

| Operation | CLI | MCP | Telegram |
|---|---|---|---|
| Ask / search / brief | ✅ | ✅ | ✅ (free text, /brief) |
| Review decisions | – (web) | ✅ | ✅ /review |
| Protect identities / suggest | ✅ | ✅ | ✅ |
| Triage stats | ✅ | ✅ | ✅ /status |
| Needs attention: list / mark seen | ✅ `needs`, `seen` | ✅ needs_me, mark_seen | ✅ /needs, /seen, 👁 Seen button (web: ✓ and Clear all) |
| Benchmark | ✅ | ❌ | ❌ |
| Sync status / verify / rescan | ✅ | partly (sync_status) | partly (/status) |
| Sender insights (cold start) | ✅ | ✅ top_contacts | ❌ |
| Unsubscribe suggestions: list / unsubscribe / keep (web: Recommendations page) | ✅ `unsubs`, `unsub`, `unsub --dismiss` | ✅ unsubscribe_suggestions, unsubscribe (confirm=true), dismiss_unsubscribe | ✅ /unsubs, 🧹 Unsubscribe / Keep buttons |
| Follow-ups ("waiting on others"): list / dismiss (also in the brief) | ✅ `followups`, `followups --dismiss` | ✅ followups, dismiss_followup | ✅ /followups, ✓ Done button |
| Natural-language questions (filters, newest, list vs answer; web: Ask box on Status) | ✅ `ask` (`--raw` = old whole-sentence search) | ✅ ask_natural (ask gains `newest`) | ✅ free text (list + 🔎 Open buttons, or answer) |
| Rules in plain language (Phase 2, slice 1; web: Rules page) | ✅ `rule add/edit/off/on/rm/show/confirm/apply`, `rules` | ✅ create_rule, confirm_rule, list_rules, show_rule, update_rule, set_rule_enabled, delete_rule; explain lists the rules that fired | ✅ /rule (✅ Save / ✖ Cancel), /rules, /rule off·on·rm·show·edit, free text ("turn off the rugby rule until February") |
| Rule dry runs over history (Phase 2, slice 2; web: read-back + Test button on Rules) | ✅ `rule test <id\|words\|"new rule">` `[--days N]`; `rule add`/`edit` print it before y/N | ✅ dry_run_rule; create_rule returns `dry_run` | ✅ /rule test 3 (or new wording); /rule read-back includes it |
| Suggested rules (Phase 2, slice 2; web: Suggested on Rules: Save / Not now / Never) | ✅ `rule suggest`, `rule suggest --accept N` / `--dismiss N` | ✅ rule_suggestions, accept_rule_suggestion, dismiss_rule_suggestion | ✅ /suggestrules (✅ Save / ✖ Never), hint in /rules, 💡 line in the brief |
| Trackers: boards (F5; web: Trackers page, home line "📦 3 in transit · 🟢 all services up · 🎟 1 on sale Fri", brief section) | ✅ `trackers`, `tracker show <id\|words>` | ✅ trackers, tracker_items (tracker?, state?, include_closed) | ✅ /trackers, /tracker show 2, push on change |
| Trackers: create / save / pause / resume / delete / test | ✅ `tracker add "<text>" [--yes]`, `tracker off\|on\|rm <id>`, `tracker test <id\|"text">`, `tracker edit` | ✅ create_tracker (read-back + dry run), confirm_tracker, set_tracker_enabled, delete_tracker, dry_run_tracker | ✅ /track (✅ Save / ✖ Cancel), /tracker off·on·rm·test, buttons on /trackers |
| Projects: list / status (Phase 3; web: Projects page + detail page, home line "🗂 3 projects · 5 open asks · next: Presentation night Fri", brief section) | ✅ `projects`, `project status\|show <name>`, `project facts <name>`, `ask "status of …"` | ✅ list_projects, project_status, project_facts | ✅ /projects, /project &lt;name&gt;, free text ("where are we with …") |
| Projects: create / save / done / archive / delete / move / file an email | ✅ `project add "<text>" [--yes] [--under P] [--item N]`, `project done\|archive\|on\|rm`, `project move <p> --under <parent>`, `project link\|unlink <id> <p>`, `project suggest` | ✅ create_project (read-back + dry run), confirm_project, set_project_status, link_to_project | ✅ /project add (✅ Save / ✖ Cancel), /project done·archive·rm, suggested sub-projects under /projects (✅ Create / ✖ Never); web: "Add to project" on every email |
| Thread status (any thread) | ✅ `thread-status <id\|"words">` | ✅ thread_status(item_id?, query?) | ✅ "status of &lt;words&gt;" when no project has that name; web: Thread status button on an email |
| Open an email from a numbered list (summary card / full text / thread; web: Summary + folded Full email on the item page, [n] on the brief page) | ✅ `show N [--full\|--thread]`, `show --item ID`; `ask`, `needs`, `brief`, `followups` print [n] | ✅ show_email(item_id, full?) | ✅ /show N, /show N full, /thread N, reply "3" to any list, 📄 Full email / 🧵 Thread buttons |
| Delivery questions / tidy up orders (migration 016) | ✅ `ask "when is my … delivery due?"`, `tracker clear-old <id> [--days N]` | ✅ tracker_items, close_tracker_item | ✅ free text, /tracker 2 clear old, ✅ Delivered button, "close the old … orders" |
| Trackers: status questions and suggestions | ✅ `ask "what's still in transit?"`, `tracker suggest [--accept N\|--dismiss N]` | ✅ trackers / tracker_items | ✅ free text ("is everything up?", "track my Acme Shop orders"), suggestions under /trackers (✅ Track / ✖ Never) |

**Query understanding (2026-10-07):** free-text questions are read into filters first (`src/emaild/query.py`): sender (fuzzy-matched against senders you actually have, so "JB Hi-Fi" finds `offers@email.jbhifi.com.au`), date window in your time zone, newest vs most relevant, how many, and whether you want a list of emails or an answer. Obvious shapes ("last 5 emails from X this week") are parsed with regexes; other questions take one schema-bound Gemma call (only your question and today's date go in, never email content), validated in Python, with the regex reading as fallback. Every reply says how it was interpreted.

**Phase 1b recommendations (2026-10-07):** unsubscribe suggestions and follow-up nudges are on every surface (table above), on the web Recommendations page, and as a line on the Status panel; the brief gains a "Waiting on others" section and an unsubscribe count.

**Rules in plain language (2026-10-07, Phase 2 slice 1):** `src/emaild/rules.py`. One Gemma call compiles your words into a small, strictly validated rule (`match` senders/addresses/domains/subject words, an optional semantic `condition.topic`, `then`/`else`, a "never archive" `floor`); senders are resolved to real addresses once, at compile time. The read-back you confirm is generated from the compiled rule, not the model's prose. Rules without a condition decide on their own (no model call); rules with one send the email to Gemma (bypassing the bulk shortcut), which only judges the condition. Security, spam and one-time codes always win; floors apply last. Soft guidance goes into the triage prompt. Decisions record the rules that fired (`decisions.rule_ids`), so `explain` always has an answer.

**Dry runs and suggested rules (2026-10-07, Phase 2 slice 2):** every read-back now ends with what the rule would have done to the last 30 days of mail ("this rule matches 41 emails: 23 would be archived (currently 18 kept, 5 already archived); 2 security-flagged — left alone"), compared with each email's final verdict (your correction over emAIl's proposal). SQL narrows the window by sender/domain, then the pure matcher decides; security, one-time and duplicate decisions are reported as left alone, and emails you reviewed differently are flagged. Rules with a condition need the model: a small sample (8 by default, 5 in a read-back, never more than 20) is checked with a tiny yes/no call (email wrapped in `<email>` tags, first 1,500 characters, never obeyed) and the rest is estimated. `rule test` runs it for an existing rule or a new wording without saving anything. Suggested rules are mined from your reviewed decisions, with no model involved: a sender (or a non-free-mail domain where two or more addresses agree) you handle the same way at least 90% of the time, at least 3 times, where emAIl got it wrong at least once (or 8+ verdicts). Groups with security/spam/one-time verdicts, groups an active rule already covers and anything you dismissed are skipped. Each suggestion is a ready-made rule text that compiles with the deterministic parser; Save creates and turns it on in one step. Suggestions refresh at most once a day (worker cycle, or lazily when listed); migration 013 adds `rule_suggestions`.

**Next:** a single command registry that generates the CLI subcommands, MCP tools and Telegram commands from one definition, so the three can't drift apart. Admin-only operations (redeploy, migrations) stay in the CLI.


---

## F5. Trackers: status boards configured in plain language

**Status: built (2026-10-07).** `src/emaild/trackers.py`, migration 014. Setup and examples: [SETUP.md §13](SETUP.md#13-trackers-f5). What shipped:
- **Kinds.** `orders` (ordered → shipped → out for delivery → delivered; side states delayed, problem, return started, cancelled, refunded), `service` (up, degraded, down, maintenance, update available; F1), `onsale` (announced → presale → general sale → sold out; cancelled), and `custom` (the user's own steps, e.g. "lodged, in review, approved or refused"). Each built-in kind has a fixed state vocabulary, a field schema and a default notify policy, which the user can widen or narrow in words ("tell me when they're delivered too").
- **Compile.** One Gemma call over only the user's words (rules machinery: strict validation, sender resolution incl. organisation domains), with a deterministic fallback for "track my X orders", "track my orders (from X)", "track X status", "(from X,) tell me when tickets (for/from Y) go on sale". The read-back is generated in Python from the compiled form, plus a 90-day dry run (items and states found; the model reads at most 8 emails for a read-back, never more than 20; the subject-line patterns give an estimate without the model). Trackers start pending and only run once saved.
- **Extraction.** One narrow, schema-bound Gemma call per email (`{is_relevant, item_key, title, state, occurred_at, fields}`; the email is wrapped in `<email>` tags, first 3,000 characters, never obeyed), validated in Python: state must be in the kind's vocabulary, links https only, dates must parse, the key must be non-empty. Fixed templates (order number + status words, service status words) are read from the subject when the model call fails.
- **Matching and state.** Items match by normalised key (order number, service/component, event name); without an identifier, a fuzzy title match over open items of the same tracker (never across trackers). Main states only move forward; side states apply any time (a "delayed" notice can't follow delivery); emails older than the item's last change are ignored as stale; repeats only refresh last-heard. Finished items leave the board (orders: delivered + 7 days, cancelled + 3, refunded at once; on-sale: sold out/cancelled + 7) and stay in history. Orders shipped 9+ days ago (out for delivery 2+) are flagged *stalled* on the board (computed, not a state).
- **Pipeline.** In the worker, after triage (so security, spam and one-time verdicts exist and win): each active tracker reads matching, triaged, safe emails from the last 30 days that it hasn't read yet (`tracker_events` records every consumed email), oldest first, at most 30 per cycle (each at most one model call). A new tracker therefore backfills its board quietly: only emails from the last 48 hours notify.
- **Archive-on-capture (conservative).** Once a *status-only* email's value is on the board (orders: shipped / out for delivery / delivered / delayed; services: any; on-sale and custom: never), emAIl's *open* proposal for it may change from keep to archive, with the reason "Captured by tracker …". Never for an event that notifies, a reviewed decision, an alert, a security / one-time / duplicate decision, a decision made by the user's own rules, or personal mail from a real person.
- **Notifications.** Telegram, outside quiet hours and not when muted, one line per change ("📦 Acme Shop order 123-456: **shipped** (expected Fri)", "🟢 Example VPN: **recovered** (was down)"). Defaults: orders notify on ordered, shipped, delayed, problem, cancelled (delivered and out for delivery are silent); services on every change (and "recovered"); on-sale on presale, general sale and cancelled, plus a reminder on the morning of each sale date; custom on every change. Services with a cadence ("it reports daily") warn when they go quiet.
- **Suggestions.** Recurring order/status mail from one non-free-mail domain (3+ in 60 days, not already tracked) is suggested ("Track my orders from acmeshop.example.com?"); Track / Not now / Never, like rule suggestions.
- **Natural language.** "track my … orders/status/tickets" creates a tracker; "what's still in transit?", "is everything up?", "any tickets going on sale soon?" are answered from the boards (no model call) when that kind is tracked, otherwise they go to the normal email search.
- **Delivery questions and aging out (2026-10-09, migration 016).** "when is my Acme Shop delivery due?", "has my order shipped?", "where's my package?", "any deliveries today?", "what orders are pending?" are answered from the open orders only (optional retailer/item filter, soonest expected first, numbered for `/show`); a past item with no open match, or no orders tracker, goes to the email search. Orders that go quiet close as *assumed delivered (no confirmation email)*: 21 days past the expected date without news, or 30 days without news and no expected date (and on the first fill, orders whose latest email is already 30 days old). They never notify, show apart from confirmed deliveries, and reopen on a later email. Manual tidy-up: Mark delivered (web, Telegram, MCP `close_tracker_item`), clear old (`/tracker 3 clear old`, `emaild tracker clear-old 3`, "close the old Acme Shop orders").

**Idea:** a lot of email is really a *state change* for something you care about: an order moving from ordered to delivered, a service going down, tickets going on sale. Instead of reading each email, you say in plain language what to track, and emAIl keeps a **board of items and their current status**. It tells you only about the changes you said matter. F1 (service status) becomes one tracker type; the machinery is shared.

**Examples**
- **Purchases.** *"Track my Amazon orders."*
  - Each order becomes a row: item, retailer, ordered date, status (ordered → despatched → out for delivery → delivered, or cancelled / return started / refunded), expected delivery, carrier and tracking link.
  - Notify on: ordered, despatched, delayed, or a problem. "Delivered" just updates the board quietly, since you already know.
  - Delivered orders drop off the board after a while but stay in history, so *"what did I buy from Amazon in August?"* and *"has the bike pump shipped?"* can be answered from it.
  - Later, the same applies to other retailers and carriers (eBay, JB Hi-Fi, Australia Post, StarTrack).
- **Services.** *"Track Twingate and Cloudflare status."* Up, down, degraded or update available, with alerts on change only (see F1).
- **Ticket and on-sale watch.** *"From Rugby Australia and the Australian Grand Prix, tell me when tickets or a ballot go on sale; archive the rest."*
  - Each event becomes a row: announced → presale → general sale → sold out, with the key dates.
  - It alerts on the sale and presale dates, and can remind you the morning of.
  - These senders bypass the bulk-mail shortcut so Gemma reads every email from them, not just the ones you've corrected.
- **Bills and renewals (later).** Due, then paid. Domain, certificate and subscription renewals with their expiry dates.

**How it would work**
1. **Defining a tracker.** You describe it in plain language (Phase 2 rule machinery). emAIl compiles the description into:
   - a *match*: senders, domains or topics;
   - a *schema* for the item: key fields plus an ordered list of states;
   - a *notify policy*: which transitions alert, which are silent.

   It reads the result back to you to confirm, and offers a dry run over the last 90 days (*"this would have created 14 orders, 3 still in transit"*). emAIl can also suggest trackers when it sees recurring templated mail ("You get order emails from Amazon; track them?").
2. **Extraction.** Each matching email gets a narrow, schema-bound Gemma call, which is easy for a small local model. It returns `{item_key, fields, state, occurred_at}`, e.g. order number, item name, status, expected date. Fixed templates can use pattern rules and skip the model. The email is always cited.
3. **Matching to an item.** The order number or event name links emails to the same row. A fuzzy match by name and date is the fallback, and is shown for confirmation when unsure.
4. **State and history.** Each item keeps its current state and a timeline of transitions. A state never goes backwards unless the email says so (cancelled, returned).
5. **Noise handling.** Once an email's state has been recorded, the email itself can be archived; that archiving goes through earned autonomy. Repeat or "no change" emails only refresh a last-heard time.
6. **Staleness.** Optionally, flag items that stall, e.g. *"despatched 9 days ago, no delivery update"* or *"Twingate silent for 3 days"*.

**Where it shows up**
- A **Trackers** page with one board per tracker (Orders, Services, Tickets…): a row per item with a status pill and when it last changed. The home page gets a small tile per tracker, e.g. "📦 3 in transit · 🟢 all services up · 🎟 1 on sale Friday".
- Alerts on Telegram follow each tracker's notify policy, outside quiet hours.
- The morning brief gets one line per tracker, covering changes only.
- Every surface (see F4):
  - MCP: `trackers()` and `tracker_items(tracker, state?)`, so you can ask "what's still in transit?" or "is everything up?"
  - CLI: `emaild trackers`
  - Telegram: `/trackers`

**Data:** three new tables, all scoped per user like everything else:
- `trackers`: the natural-language definition, the compiled match, schema and notify policy, and an enabled flag
- `tracker_items`: the key, fields and current state
- `tracker_events`: transitions, each linked to its source email

**Relation to other plans**
- It's built on Phase 2 rules and supersedes F1 as a one-off. Slice 1 of rules is in (2026-10-07): a tracker's *match* will reuse the compiled rule `match` and the pure matcher in `rules.py`, and the ticket-watch example already works as a plain rule (alert on sale / archive the rest) until the board exists.
- The ticket watch also covers the "interest senders" stopgap discussed on 2026-10-07: senders you watch for something specific are read by Gemma rather than archived by the bulk shortcut.
- Phase 3's fact extraction (e.g. keeping a favourite newsletter author's emails as a knowledge base) uses the same "narrow schema per email" pattern, but builds notes rather than a status board.


---

## F7. Draft emails (written by emAIl, never sent)

**Idea:** ask in plain language — *"draft a reply to the club treasurer saying I can do the canteen on Saturday"*, *"draft a follow-up to the venue about the 14th"* — and emAIl writes the email. It lands as a **draft** you review, edit and send yourself. emAIl never sends anything.

**Permissions: not possible with today's read-only access.** Creating a draft writes to the mailbox, so it needs one extra permission per provider:

| Provider | Today | Needed for drafts | Notes |
|---|---|---|---|
| Gmail | `gmail.readonly` | `gmail.compose` | The narrowest Gmail scope that can create drafts. Google bundles "send" into the same scope, so "never send" is enforced in emAIl's code (it never calls the send endpoint), not by the permission itself. |
| Outlook.com (Graph) | `Mail.Read` | `Mail.ReadWrite` | Lets emAIl create a draft (it also allows moving or deleting mail, again limited by code). **Sending needs the separate `Mail.Send`, which emAIl never requests**, so on Outlook drafts are send-proof by permission. |

Adding a scope means re-linking each account once (consent screen). It should be opt-in per account (`EMAILD_DRAFTS=1`, plus a per-account toggle), so read-only stays the default.

**Option A — no new permissions (can ship first):** emAIl keeps drafts in its own database and shows them on the dashboard, in Telegram and via MCP, with one-tap **"Open in Gmail"** / **"Open in Outlook"** links that open a pre-filled compose window (to, subject, body), plus Copy. Limitation: a pre-filled compose window starts a *new* email, so replies won't sit in the original thread.

**Option B — real drafts in the mailbox (with the extra scope):** the draft appears in the account's Drafts folder, correctly threaded as a reply (In-Reply-To/References headers, same conversation), ready to send from any device.

**How drafting would work (both options)**
1. **Understand the request** with the query-understanding step: who it's to (resolved against senders you know), what it replies to (the right thread), and what to say.
2. **Write it** with Gemma (local), using the thread for context and, later, examples of your own sent mail so it sounds like you (Phase 6, "in your voice"). Email content stays untrusted: instructions inside the emails being replied to are never followed.
3. **Show it for approval** with a read-back (to, subject, first lines) and Edit / Regenerate / Save-as-draft / Discard. Nothing leaves emAIl until you choose.
4. **Safety rails:** never auto-send (no send endpoint is ever called, and on Outlook the permission isn't even held); drafts only to addresses you've received mail from or typed explicitly; replies to anything flagged as phishing or suspicious are refused; every draft is audited.

**Surfaces:** dashboard (Draft button on an email and a "New draft" box), Telegram (`/draft …`, plus "✍️ Draft reply" on alert cards), MCP (`draft_email`, `draft_reply(item_id, instructions)`), CLI (`emaild draft "..."`).

**Relation to other plans:** this is the start of Phase 6 (drafts in your voice). Option A is independent of Phase 4's write access; Option B shares its re-consent step with F2 (deleting expired codes needs `gmail.modify` anyway — if both are wanted, `gmail.modify` covers drafts too, so one re-link would do).


---

## F8. Deterministic-first triage (fast path ahead of the LLM)

**Idea:** most triage decisions don't need a language model. A sender you always archive, a newsletter you never open, a club you always keep — these are predictable from structured signals and your own past verdicts. Decide those in about a millisecond, explainably, and send only the uncertain or language-heavy cases to Gemma. Fewer model calls, faster cycles, and a clear "why" for every decision.

**Reviewed and rejected: Laya** (github.com/NandhaKishorM/laya, reviewed at v0.3.29). Despite the "decision system" label it is a 322M–421M-parameter neural text classifier, near chance before fine-tuning (by its own README), prone to collapsing to the majority class on a few hundred rows, text-only (our best signals are structured), probability-only (no per-feature "why"), and adds torch plus a ~1.7 GB model to the container. **Ideas borrowed from it:** per-action abstention thresholds that fail closed, an explicit decided / deferred / not-evaluated state per email, and shadow → compare → promote adoption.

**Proposed design**
- **Where it sits:** security → one-time codes → duplicates → user rules → **fast path** → Gemma → guards/floors → review policy. Safety checks and your rules keep priority; guards still apply after the fast path.
- **What it is:** a calibrated logistic-regression model over hand-built features (sender history, list/bulk headers, labels, auth results, rule matches, account, time patterns) plus a k-nearest-neighbour vote over the in-database MiniLM embeddings of your reviewed emails (the same retrieval `find_examples` already does). Sub-millisecond per email; trains from the `decisions` table in under a second; adds only scikit-learn. (Oracle's in-database ML is an alternative if available in 26ai Free — to confirm.)
- **Abstains unless sure:** it only decides keep/archive when calibrated confidence clears a per-action threshold; alerts, low-confidence and genuinely language-dependent emails go to Gemma as today.
- **Explainable:** `explain` shows the top features and nearest past verdicts, e.g. *"archive: newsletter headers, you archived 14 of 14 from this sender"*.
- **Stays on Gemma permanently:** content summaries, project facts, thread status, rule topic checks, answers to questions, and compiling plain-language rules/trackers/projects.

**Prerequisite — an honest benchmark (Phase A, ~half a day)**
The review found probable label leakage: `sender_keeps`, `sender_overridden`, `sender_cleared`, `find_examples` and sender stats can count the email being scored (or replies sent after it). Fix with point-in-time ("as of") exclusions, make `benchmark` run the full `decide()` pipeline, report precision / coverage / hidden-wanted per action and per decision source, and count LLM calls per cycle from `llm_calls` — then re-baseline Gemma.

**Rollout**
1. Phase A — benchmark fix and baseline (above).
2. Phase B — feature view + model training (migration for model/metrics storage), `fastpath.py`, `EMAILD_FASTPATH=off|shadow|on` (default `off`).
3. Phase C — **shadow mode**: run alongside Gemma, log both, compare in the benchmark.
4. Phase D — switch on **per action** once precision clears the bar on reviewed mail; demote automatically if spot checks slip. This is the natural on-ramp to Phase 4 (earned autonomy).
5. Later — consider the same pattern for tracker state extraction (templated status emails) and project sub-project choice.

**Note (2026-10-09):** with work or school Microsoft 365 accounts now linkable (Phase 5), mail volume per user can be several times higher, and a lot of work mail is templated (notifications, approvals, automated reports). That makes the fast path more valuable: fewer Gemma calls per cycle, and work accounts default to local models only, so there's no cloud fallback to absorb the load.

**Metrics to watch:** LLM calls per cycle, latency per email, action accuracy, hidden-wanted (archived something you'd keep), review volume, fast-path coverage.

Full review and plan: `emAIl-laya-review.md` in the project docs.

