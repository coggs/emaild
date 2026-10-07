# emAIl — Feature list

This is the backlog of features beyond the phased roadmap in the design spec. Each entry says what the feature is, how it would work, and roughly when it fits.

| # | Feature | Status | Fits after |
|---|---|---|---|
| F1 | Service status monitoring | Proposed (2026-10-06); now the first tracker type in F5 | Phase 2 (rules) |
| F2 | Delete expired one-time codes / sign-in links in Gmail | Proposed (2026-10-07) | Phase 4 (write access) |
| F3 | Protected identities: web page + suggestions | Proposed (2026-10-07) | Phase 2 |
| F4 | One command surface: CLI, MCP and Telegram parity | In progress (2026-10-07) | ongoing |
| F5 | Trackers: status boards configured in plain language (orders, services, ticket releases…) | Proposed (2026-10-07); builds on rules (slice 1 done) | Phase 2 (rules) |
| F6 | Generic IMAP connector (app passwords: iCloud, Fastmail, ISP mailboxes) | Possible later addition | after Phase 5 |

**Phase 5, Outlook.com (2026-10-07): delivered** through Microsoft Graph (`src/emaild/channels/outlook.py`). Read-only `Mail.Read` over OAuth (auth code + PKCE, `consumers` authority). It uses per-folder delta sync for Inbox, Sent, Junk and Archive, and fetches raw MIME through the same parser as Gmail. Outlook state is mapped onto the Gmail label names, so triage, search and briefs work unchanged. Setup is in `docs/SETUP.md` (5b). A generic IMAP connector (F6) could later cover providers that still allow app passwords (iCloud, Fastmail, many ISPs). Outlook.com can't use it because Microsoft turned off basic auth there in September 2024.

---

## F1. Service status monitoring

*Generalised by [F5](#f5-trackers-status-boards-configured-in-plain-language): service status is one tracker type among several.*

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
| Rule dry runs over history (Phase 2, slice 2) | planned | planned | planned |

**Query understanding (2026-10-07):** free-text questions are read into filters first (`src/emaild/query.py`): sender (fuzzy-matched against senders you actually have, so "JB Hi-Fi" finds `offers@email.jbhifi.com.au`), date window in your time zone, newest vs most relevant, how many, and whether you want a list of emails or an answer. Obvious shapes ("last 5 emails from X this week") are parsed with regexes; other questions take one schema-bound Gemma call (only your question and today's date go in, never email content), validated in Python, with the regex reading as fallback. Every reply says how it was interpreted.

**Phase 1b recommendations (2026-10-07):** unsubscribe suggestions and follow-up nudges are on every surface (table above), on the web Recommendations page, and as a line on the Status panel; the brief gains a "Waiting on others" section and an unsubscribe count.

**Rules in plain language (2026-10-07, Phase 2 slice 1):** `src/emaild/rules.py`. One Gemma call compiles your words into a small, strictly validated rule (`match` senders/addresses/domains/subject words, an optional semantic `condition.topic`, `then`/`else`, a "never archive" `floor`); senders are resolved to real addresses once, at compile time. The read-back you confirm is generated from the compiled rule, not the model's prose. Rules without a condition decide on their own (no model call); rules with one send the email to Gemma (bypassing the bulk shortcut), which only judges the condition. Security, spam and one-time codes always win; floors apply last. Soft guidance goes into the triage prompt. Decisions record the rules that fired (`decisions.rule_ids`), so `explain` always has an answer. Dry runs over history come in slice 2 (the matcher is a pure function, ready for it).

**Next:** a single command registry that generates the CLI subcommands, MCP tools and Telegram commands from one definition, so the three can't drift apart. Admin-only operations (redeploy, migrations) stay in the CLI.


---

## F5. Trackers: status boards configured in plain language

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
