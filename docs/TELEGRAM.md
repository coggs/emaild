# Telegram commands

Everything the emAIl Telegram bot understands. Setup (bot token, linking a chat) is in [SETUP.md §10](SETUP.md#10-phase-1b1c--morning-brief-and-telegram).
Anything you type that isn't a command is treated as a **question about your email**. Examples below use made-up names.

The same operations are available on the dashboard, the `emaild` command line and MCP (see the parity table in [FEATURES.md](FEATURES.md#f4-one-command-surface-cli-mcp-and-telegram)).

## Everyday

| Command | What it does |
|---|---|
| `/brief` | A brief of what's come in since the last one. The morning brief also arrives on its own at `EMAILD_BRIEF_TIME`. |
| `/status` | Sync and triage at a glance: accounts, messages, how many proposals are waiting for you. |
| `/needs` | What needs you: alerts, plus emails still waiting on your reply, numbered — `/show 2` opens one. |
| `/seen` | Clear everything from Needs attention (your triage verdicts are unchanged). Or tap **👁 Seen** on a single alert. |

## Ask your email

No command needed — just type. emAIl works out the sender, dates, newest vs most relevant, and whether you want a **list** or an **answer**, and ends each reply with an *Interpreted as …* line.

| You type | You get |
|---|---|
| `last 5 emails from Sam Taylor` | A numbered list ([1], [2], …), newest first, with a one-line summary and an **🔎 Open** button for each. Dates work too: "anything from the school this week", "emails from Sam in August". `/show 2` opens one (see [Opening emails](#opening-emails)). |
| `what are the latest offers from Acme Streaming?` | An answer written from the newest matching emails, with sources. |
| `messages from the NSFC committee` | Group words (committee, team, office, board, staff…) mean everyone at that organisation's domain, not one mailbox. |

Spam, suspicious mail and one-time codes are never included.

## Opening emails

Every message that lists emails numbers them **[1], [2], …** — question results and answer sources, `/brief` (one running numbering for the whole brief), `/needs`, `/followups`, delivery answers and tracker boards (each item's latest email), and project or thread status citations. Each list remembers its own numbers, so you can come back to an older one.

| You send | You get |
|---|---|
| `/show 3` | A summary card for email [3] of the last list: subject, sender, date (your time zone), what it says, and key details pulled from it — dates and times, amounts, how many links and their first three domains (never the links themselves), attachment names. Buttons: **📄 Full email · 🧵 Thread · 🔎 Open** (Open only when the dashboard is on https). |
| `/show 3 full` | The whole email as plain text, split over at most 3 messages; longer ones end with "truncated — open on dashboard". Attachments are listed by name, never sent. |
| `/thread 3` | Where that email's conversation stands (who's waiting on whom, decisions, open asks, dates). |
| *reply* `3` · `show 3` · `full 3` · `thread 3` | Reply to any list message to pick from **that** list, even an older one. |
| `3` *(no reply)* | Opens [3] of the latest list, but only if that list is less than 30 minutes old; otherwise it's treated as a normal question. |

The whole email is refused for spam, suspicious, one-time-code and security-held mail (and expired codes emAIl has scrubbed), and at `/detail minimal` — `/detail summary` allows it. Every full view is logged.

## Review

| Command | What it does |
|---|---|
| `/review` | Triage proposals waiting for you, one card at a time: **✅ Right · 🔔 Alert · 📌 Keep · 🗄 Archive**. The next card follows. |
| *(reply to "Why?")* | After a correction the bot asks why. Reply to that message with a short reason; it guides similar emails in future. |
| `/refresh` | Re-check the review list against the latest rules and the spam, impersonation and one-time-code checks. |

## Rules

Rules are written in plain words. Each one is read back to you with a preview of what it would have done in the last 30 days, and only switches on when you tap **✅ Save**. Refer to a rule by its number or by a word from its name.

| Command | What it does |
|---|---|
| `/rule <your rule in words>` | Create a rule (**✅ Save / ✖ Cancel**). e.g. `/rule Always archive emails from Acme Streaming` or `/rule From NSFC, alert me when tickets or a ballot go on sale; archive the rest` |
| `/rule guidance: <text>` | Soft guidance added to the model's instructions rather than a hard rule. e.g. `/rule guidance: I care less about conference marketing unless I'm speaking` |
| `/rules` | Your rules with status and how often they've fired (**Off / On / Delete** buttons). |
| `/rule show 3` | The rule's read-back and version history. |
| `/rule edit 3 <new wording>` | Reword a rule. It's read back again and stays off until you Save. |
| `/rule off 3 [until February]` | Turn a rule off, optionally until a date ("until 15 March", "for 2 weeks"). `/rule pause 3` does the same. |
| `/rule on 3` | Turn a rule back on. |
| `/rule rm 3` | Delete a rule (`/rule delete 3` also works). |
| `/rule test 3` | What rule 3 would have done to the last 30 days, changing nothing. `/rule test <rule in words>` tests one you haven't saved. |
| `/suggestrules` | Rules emAIl suggests from your reviews (**✅ Save / ✖ Never**). `/rulesuggestions` also works. |

Plain words work too: "show my rules", "turn off the acme rule until February", "delete the streaming rule".

## Trackers

Boards for orders, service status and ticket sales, set up in plain words. Each tracker is read back to you with what it would have found in the last 90 days, and only starts when you tap **✅ Save**. Refer to a tracker by its number or a word from its name.

| Command | What it does |
|---|---|
| `/track <what to track>` | Create a tracker (**✅ Save / ✖ Cancel**). e.g. `/track Track my Acme Shop orders`, `/track Track Example VPN and Example CDN status`, `/track From NSFC, tell me when tickets or a ballot go on sale` |
| `/trackers` | Your boards: each item's state (🟢 🟠 🔴), the date that matters (expected delivery, next sale) and ⚠ stalled orders, with **Pause / Resume / Delete** buttons. Suggested trackers follow (**✅ Track / ✖ Never**). |
| `/tracker show 2` | One board in full, with its read-back and your words. |
| `/tracker off 2` · `/tracker on 2` · `/tracker rm 2` | Pause, resume or delete a tracker (`pause`, `resume`, `delete` also work). |
| `/tracker test 2` | What tracker 2 finds in the last 90 days, changing nothing. `/tracker test <tracker in words>` tests one you haven't saved. |
| *(automatic)* | Changes that matter arrive on their own, e.g. "📦 Acme Shop order 123-456: **shipped** (expected Fri)", "🟢 Example VPN: **recovered** (was down)", a reminder on the morning a sale opens. Quiet hours and `/mute` apply. |

| `/tracker 2 clear old` | Close tracker 2's open orders with no news for 21+ days, as *assumed delivered* (`/tracker 2 clear old 30` for another number of days). A later email about one puts it back. |
| **✅ Delivered** | Under delivery answers and `/tracker show`: you got it — the order leaves the board. |

Plain words work too: "track my Acme Shop orders", "what's still in transit?", "is everything up?", "any tickets going on sale soon?", "show my trackers", "close the old Acme Shop orders". If you're not tracking that kind of thing, the question goes to your email as usual.

Delivery and order questions — "when is my Acme Shop delivery due?", "when will my parcel arrive?", "has my order shipped?", "where's my package?", "any deliveries today?", "what orders are pending?" — are answered from your **open** orders (just that retailer's when you name one), soonest expected first, numbered, with the state, expected date, last update and a ⚠ when one has gone quiet. A question about something that's no longer on the board ("when did the bike pump arrive?") goes to your email instead. Orders that never got a delivery email leave the board on their own after a while, as *assumed delivered (no confirmation email)*, without a notification.

## Projects

Projects group the mail of something you're involved in. An *umbrella* is ongoing (a club committee, the household); its *sub-projects* are goals with an end (a presentation night, a uniform order). emAIl files matching emails, notes decisions, asks of you, commitments, deadlines and open questions (each citing its email), and tells you where things stand. Each project is read back to you with what it would have filed in the last 90 days and only starts when you tap **✅ Save**.

| Command | What it does |
|---|---|
| `/project add <what>` | Create a project (**✅ Save / ✖ Cancel**). e.g. `/project add Create a project for the NSFC committee, everything from nsfc.example.org`, `/project add Add a sub-project under NSFC: presentation night`, `/project add Track my kitchen renovation with the builder at builder.example.com` |
| `/projects` | Your umbrellas and sub-projects: open asks, the next date, last activity. Suggested sub-projects follow (**✅ Create / ✖ Never**). |
| `/project NSFC` | Where a project stands: sub-project one-liners and general business (umbrella) or asks / deadlines / commitments / decisions / open questions (sub-project), upcoming dates, who's waiting on whom, and a short overview written from the facts. Each fact cites its email as [1], [2], …; `/show 1` opens it. |
| `/project done NSFC` · `/project archive NSFC` · `/project rm NSFC` · `/project on NSFC` | Mark done, archive (filing stops, history stays), delete (sub-projects move up a level), or reopen. |

Plain words work too: "status of presentation night", "where are we with the kitchen renovation?", "what's happening with NSFC?", "show my projects", "add a sub-project under NSFC: uniform order". If no project has that name, emAIl finds the best-matching email thread and tells you where *it* stands instead. Only facts and summaries are sent here, never the emails themselves.

## Impersonation protection

| Command | What it does |
|---|---|
| `/protect Alex Rivera \| example.org` | Protect a person's name: mail using it from any other address is flagged as possible phishing. |
| `/protectorg NSFC \| nsfc.example.org` | Protect an organisation, including its committee roles (president, treasurer…). |
| `/unprotect Alex Rivera` | Remove a protected name. |
| `/protected` | List protected names and their allowed addresses or domains. |
| `/suggest` | Suggestions: role names seen in display names, and the organisation domains your mail is addressed to. |

## Recommendations

| Command | What it does |
|---|---|
| `/unsubs` | Lists you could unsubscribe from (**🧹 Unsubscribe / Keep**). Never automatic, never for spam or phishing. |
| `/followups` | Emails you sent that are still waiting on a reply, numbered, with a **✓ Done** button each. |

## Settings

| Command | What it does |
|---|---|
| `/detail minimal\|summary\|full` | How much the bot shows in this chat. |
| `/mute` | Pause alerts. The morning brief still comes. |
| `/unmute` | Alerts back on. |

## Setup

| Command | What it does |
|---|---|
| `/link 123456` | Link this chat using a one-time code from the dashboard's Telegram page. Codes expire after 10 minutes. |
| `/unlink` | Stop all messages to this chat. |
| `/help` | Show the command list (`/start` does the same). |

## Buttons on messages

Nothing to type — these appear on cards the bot sends.

| Where | Buttons |
|---|---|
| New alert cards | **✅ Right · 🔔 Alert · 📌 Keep · 🗄 Archive**, **👁 Seen** (clear from Needs attention) and **🔎 Open original** (opens the email on your dashboard, when `EMAILD_PUBLIC_URL` is https). |
| One-time codes | Pushed within seconds of arrival, even in quiet hours. The code itself is only shown if `EMAILD_TELEGRAM_SHOW_CODES=1`. |

## Privacy

Telegram bot chats are not end-to-end encrypted. The bot sends summaries (sender, subject, a one-line summary, at your `/detail` level). A whole email is sent only when you explicitly ask for it (`/show N full` or **📄 Full email**); it is refused for spam, suspicious, one-time-code and security-held mail, and at `/detail minimal`, and every full view is logged. Attachments are never sent, and one-time codes only if you opt in.
