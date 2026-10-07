# Phase 0 setup — Ubuntu 24.04 on WSL2 + rootless Podman

Run everything from the Ubuntu shell in `/path/to/emAIl`. You need Podman 4.9+, podman-compose, systemd enabled in WSL, and Ollama reachable at `localhost:11434` from WSL (check: `curl -s localhost:11434/api/tags`).

Expect about 30 minutes, most of it the database initialising on first start.

## How the networking works

- The `db` container publishes 1521 on WSL's localhost.
- The `api`, `worker` and `mcp` containers use **host networking**, so for them `localhost` is WSL itself: the DB at `localhost:1521` and Ollama at `localhost:11434`.
- WSL forwards localhost ports to Windows, so `http://localhost:8080` opens from your Windows browser. Google's OAuth redirect relies on this.

## 0. The `emaild` command (do this first)

```bash
cd /path/to/emAIl && bash scripts/install-cli.sh
```
From then on, every command in this guide is just `emaild <command>`. It runs inside the running api container, or in a one-off container if the stack is down. `emaild help` lists everything. Host helpers:
- `emaild up` / `emaild down` / `emaild ps`
- `emaild logs [worker|api|mcp|telegram|db]`
- `emaild redeploy`

## 1. Configure

```bash
cd /path/to/emAIl
cp .env.example .env
python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Edit `.env`:
- Paste that value into `EMAILD_MASTER_KEY`. **Back it up.** Without it, the stored tokens and raw mail can't be read.
- Set strong values for `ORACLE_PWD` and the three `EMAILD_DB_*_PASSWORD` settings, with no double quotes.
- Set `EMAILD_DEFAULT_USER` to your email address.
- `EMAILD_LLM_MODEL=gemma4:latest` is already the default.

## 2. Google OAuth client (one-off)

1. https://console.cloud.google.com → create a project (e.g. `emaild`).
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **OAuth consent screen** (Google Auth Platform):
   - External; app name `emAIl`; your email as the contact.
   - Add the scope `.../auth/gmail.readonly`.
   - Add yourself as a test user.
4. **Clients → Create client → Web application**. Authorised redirect URI: `http://localhost:8080/oauth/google/callback`.
5. Download the JSON and save it as `secrets/google_client.json`.

> **Token lifetime:** while the consent screen is in *Testing*, Google expires refresh tokens after 7 days. For personal use, set the publishing status to *In production* without verifying the app. You'll see an "unverified app" warning once when linking (*Advanced → Go to emAIl*), and the tokens then stop expiring.

## 3. Start the database

```bash
podman compose up -d db
podman logs -f emaild_db_1        # check the name with `podman ps`
```
Wait for `DATABASE IS READY TO USE!` (5–15 minutes on first start), then press Ctrl+C.

## 4. Build, create the schema, load the embedding model

```bash
podman compose build
emaild init-db
```

Download Oracle's prebuilt embedding model into `models/` and unzip it:
```bash
cd models
curl -LO "https://adwc4pm.objectstorage.us-ashburn-1.oci.customer-oci.com/p/TtH6hL2y25EypZ0-rrczRZ1aXp7v1ONbRBfCiT-BDBN8WLKQ3lgyW6RxCfIFLdA6/n/adwc4pm/b/OML-ai-models/o/all_MiniLM_L12_v2_augmented.zip"
unzip -o all_MiniLM_L12_v2_augmented.zip && ls *.onnx      # expect all_MiniLM_L12_v2.onnx
cd ..
emaild load-model
emaild check-llm
```

## 5. Run everything and link Gmail

```bash
podman compose up -d
```
Open **http://localhost:8080** in your Windows browser → **Link a Gmail account** → sign in and allow read-only access.

The worker backfills `EMAILD_BACKFILL_DAYS` of mail (default 180), 400 messages per 90-second cycle, then switches to incremental polling. Embedding runs behind it on the database's 2 CPUs, and the page shows the backlog.

```bash
podman logs -f emaild_worker_1
emaild status
emaild search "BAS lodgement"
emaild ask "what did my accountant say about the BAS?"
emaild ask "last 5 emails from <USER NAME>"
```

`ask` (and Telegram free text, the **Ask your email** box on the Status page, and the MCP `ask_natural` tool) first works out what you mean: who the mail is from, which dates, newest or most relevant, how many, and whether you want a **list** of emails or an **answer**. *"show me the last 5 emails from <user name>"* lists his five newest; *"what are the latest perks from JB Hi-Fi?"* answers from the newest JB Hi-Fi mail (the name matches `jbhifi` addresses too); *"anything from <organisation> this week"* and *"what did the accountant say about BAS in August"* work the same way. Dates such as "this week" or "in August" are days in `EMAILD_TZ`. Each reply ends with an *Interpreted as …* line so you can see how it was read; `emaild ask --raw "..."` skips this and searches on the whole sentence as before. Spam, suspicious mail and one-time codes are never included.

## 5b. Linking an Outlook.com account

Outlook.com, Hotmail and Live mailboxes (personal Microsoft accounts) are read through Microsoft Graph with OAuth. Microsoft turned off basic authentication and app passwords for Outlook.com in September 2024, so IMAP with a password is no longer an option; emAIl asks only for read-only access (`Mail.Read`).

1. Sign in to https://portal.azure.com with any Microsoft account that has an Entra ID directory (a free Azure account creates one). This doesn't have to be the mailbox you'll link.
2. **Microsoft Entra ID → App registrations → New registration**:
   - Name: `emAIl`.
   - Supported account types: **Personal Microsoft accounts only**. *Accounts in any organizational directory and personal Microsoft accounts* also works. emAIl signs in through the `consumers` authority (`EMAILD_MS_TENANT=consumers`), which accepts personal accounts under either option. Single-tenant ("this organizational directory only") does **not** work for Outlook.com.
   - Redirect URI: platform **Web**, `http://localhost:8080/oauth/microsoft/callback`. Behind a reverse proxy, use `https://<YOUR_DOMAIN>/oauth/microsoft/callback` instead (or add both under **Authentication**). It must equal `${EMAILD_PUBLIC_URL}/oauth/microsoft/callback` exactly.
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**: `Mail.Read`, `User.Read`, `offline_access`. Don't add `Mail.ReadWrite` or `Mail.Send`. Personal accounts consent for themselves when linking, so no admin consent is needed.
4. **Certificates & secrets → New client secret**. Copy the secret's **Value** (not its Secret ID) straight away, because it's shown only once. Secrets expire (6–24 months). Put the expiry date in your calendar and create a new one before then. When it expires, sync stops with a token-endpoint error until you update `.env`.
5. In `.env`, set `EMAILD_MS_CLIENT_ID=<Application (client) ID>` from the app's Overview page. For the secret, either set `EMAILD_MS_CLIENT_SECRET=<secret value>` or save it as `secrets/ms_client_secret`. Leave `EMAILD_MS_TENANT=consumers`.
6. Apply the migration and recreate the containers so they read the new settings:
   ```bash
   emaild migrate
   podman compose up -d --force-recreate api worker
   ```
7. Open the Status page → **Link an Outlook account** → sign in with the Outlook.com mailbox and accept the read-only permissions. The Accounts table shows it with provider *Outlook.com*.

What syncs: Inbox, Sent Items, Junk Email and Archive (if you have one), for the same `EMAILD_BACKFILL_DAYS` window as Gmail, and then changes every cycle. Outlook's state is mapped onto the labels emAIl already uses: Junk → spam, Deleted Items → trash, Sent → sent, flagged → starred, unread, high importance → important. Focused Inbox's *Other* becomes `CATEGORY_OTHER`, which is a hint only and isn't treated as promotions. Moving a message to Junk in Outlook reclassifies it as spam in emAIl, just like Gmail's Spam label.

If Microsoft stops accepting the sign-in (password change, revoked consent, or 90 days without a sync), the account shows **reauth** with "Microsoft sign-in expired — relink the account". Click **Link an Outlook account** again. Sync picks up where it stopped.

## 6. Connect an MCP client

**HTTP:** `http://localhost:8081/mcp`. If `EMAILD_MCP_TOKEN` is set, send `Authorization: Bearer <token>`.

**Claude Desktop (stdio, through WSL into the running container)**, in `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "emAIl": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu-24.04", "podman", "exec", "-i", "emaild_mcp_1",
               "emaild", "mcp", "--transport", "stdio"]
    }
  }
}
```

## 6b. LAN access through a reverse proxy (e.g. Nginx Proxy Manager)

1. In `.env`:
   - `EMAILD_BIND_HOST=0.0.0.0`
   - `EMAILD_WEB_PASSWORD=<password>`. The browser asks for it; any username works.
   - `EMAILD_MCP_TOKEN=<token>`
   - `EMAILD_PUBLIC_URL=https://<YOUR_DOMAINNAME>`

   Then recreate the api and mcp containers.
2. Google OAuth client: add `https://<YOUR_DOMAINNAME>/oauth/google/callback` as a redirect URI.
   Microsoft app registration (if you link Outlook.com): add `https://<YOUR_DOMAINNAME>/oauth/microsoft/callback` as a Web redirect URI.
3. In Windows (PowerShell as Administrator), forward the ports into WSL's NAT IP (`hostname -I`) and allow them through the firewall:
   ```powershell
   netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8088 connectaddress=<wsl-ip> connectport=8088
   netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8081 connectaddress=<wsl-ip> connectport=8081
   New-NetFirewallRule -DisplayName "emAIl web 8088" -Direction Inbound -Protocol TCP -LocalPort 8088 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "emAIl MCP 8081" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow -Profile Private
   ```
   Tests run on the PC itself bypass the firewall. Check from another device with `http://<pc-lan-ip>:8088/healthz`.
4. In NPM, point the proxy host at `http://<pc-lan-ip-or-wsl-ip>:8088`.

The WSL NAT IP can change after a full WSL restart. If it does, update the portproxy rules.

## 7. Surviving reboots

Rootless Podman only restarts `restart: always` containers through a user service, and WSL only runs while something keeps it up.

```bash
systemctl --user enable --now podman-restart.service
sudo loginctl enable-linger $USER
```

Then, in Windows Task Scheduler, add a task that runs **At log on** with this action:
```
powershell.exe -WindowStyle Hidden -Command "wsl.exe -d Ubuntu-24.04 --exec bash -lc 'cd /path/to/emAIl && podman compose up -d && exec sleep infinity'"
```
The `sleep infinity` keeps the WSL VM alive, so the worker keeps polling after you close your terminals.

## 9. Phase 1a — triage (shadow mode)

After `bash scripts/redeploy.sh` (which applies migration 006), the worker triages new mail every cycle, plus the last `EMAILD_TRIAGE_DAYS` (default 14) of existing mail, `EMAILD_TRIAGE_PER_CYCLE` emails at a time.

```bash
emaild cold-start        # who you engage with, learned from your sent mail
emaild triage --limit 10 # triage a batch now instead of waiting for the worker
emaild triage-stats
```

- **Review:** open the **Review** page in the web app. Approve what's right, and correct what isn't. Adding a short reason helps most.
- **From an MCP client:** ask it to *"walk me through my pending decisions"*. That uses the `pending_decisions` and `review_decision` tools.
- **Benchmark:** once you've reviewed 30–50 emails, measure how well a model agrees with you:

```bash
emaild benchmark                     # current triage model
emaild benchmark --model qwen3-coder:30b   # or try another Ollama model
```

## 10. Phase 1b/1c — morning brief and Telegram

1. In Telegram, message **@BotFather** with `/newbot`. Put the token it gives you in `.env` as `EMAILD_TELEGRAM_TOKEN=…`.
2. Check the brief settings in `.env`:
   - `EMAILD_TZ=Australia/Sydney`
   - `EMAILD_BRIEF_TIME=08:00`
   - `EMAILD_BRIEF_DAYS=mon,…,sun`
   - `EMAILD_QUIET_HOURS=22:00-07:00`. Alerts that arrive in this window are held until it ends.
3. Run `bash scripts/redeploy.sh`. This applies migration 007 and starts the new `telegram` container.
4. Open the **Telegram** page in emAIl, click **Get a link code**, then send `/link 123456` to your bot within 10 minutes.

**Bot commands:**
- `/brief`
- `/review`: cards with ✅ Right / 🔔 Alert / 📌 Keep / 🗄 Archive. After a correction, reply with a reason.
- `/status`
- `/detail minimal|summary|full`
- `/mute`, `/unmute`
- `/unlink`
- Any other message is treated as a question about your email.

**Privacy:** Telegram bot chats are not end-to-end encrypted. The bot only sends summaries (sender, subject, a one-line summary, at your chosen detail level), never full emails. The **Open original** button links to your LAN-only web app.

**Without Telegram:** the **Brief** page in the web app, `emaild brief`, and the MCP tools `brief` and `needs_me`.

**Recommendations (Phase 1b, migration 010).** The **Recommendations** page (and `/unsubs`, `/followups`, `emaild unsubs`, `emaild followups`, and the matching MCP tools) suggests:
- **Unsubscribes:** list mail (it has a `List-Unsubscribe` header) from senders you've never replied or written to, never kept or alerted on, and mostly file as noise. Nothing is ever automatic. **Unsubscribe** does an RFC 8058 one-click HTTPS POST to the sender's endpoint (public hosts only); when a sender only offers a web page or an email address, emAIl hands you the link to finish yourself (it never opens unsubscribe pages and never sends email). Spam/suspicious senders and protected identities are never suggested: unsubscribing from spam confirms your address. **Keep getting these** stops the suggestion for good.
- **Follow-ups ("waiting on others"):** emails you sent 3-21 days ago that ask something (a question, "let me know", "could you", "any update"...) of a real person, with nothing back in the thread since. They also appear in the brief. Tick ✓ when it's handled.

## 11. Security, one-time codes and impersonation

**Spam and phishing.** Gmail's Spam label, failed sender authentication (DMARC/SPF), and impersonation are all handled before any other rule or the model. Impersonation means a known contact's name, or a protected name, arriving from the wrong address. These emails never appear in briefs, search or answers. Suspicious ones go to Review with the reason.

**Protect people and organisations that phishers impersonate:**
```bash
emaild protect "<USER NAME>" --allow president@<organisation>.org.au --allow <organisation>.org.au --note "club president"
emaild protect "<ORGANISATION>" --org --allow <organisation>.org.au
emaild protect          # list
```
Each `protect` re-checks the last 30 days. For an organisation, any committee-role sender (President, Treasurer, Secretary, Registrar, Chair…) is also checked: one tied to the club by its name, or by being sent to a club address, must come from the club's domain. A bare role name from a personal mailbox (gmail, outlook…) you've never corresponded with is always flagged.

To find the real domains and the fakes in your own mail:
```bash
emaild protect --suggest
``` You can also do this from MCP: *"protect <USER NAME>, he only emails from president@<organisation>.org.au"*.

**One-time codes and sign-in links:**
- Detected as they arrive, and sent to Telegram straight away, even during quiet hours, with the expiry time.
- Hidden from everything once expired.
- About 10 minutes after expiry, emAIl's own copy is scrubbed: the text, the search index and the stored original. Gmail isn't touched.
- The code itself isn't sent to Telegram unless you set `EMAILD_TELEGRAM_SHOW_CODES=1` (not recommended).
- For quick delivery, set `EMAILD_POLL_SECONDS=30`.

## 12. Phase 2 — rules in plain language

Apply migration 011 first: `emaild migrate`.

Say how you want mail handled, in your own words. emAIl compiles it (one local Gemma call, only your words go in), reads it back in plain English, and it does nothing until you save it.

```bash
emaild rule add "From Rugby Australia or the Australian Grand Prix, alert me when tickets or a ballot go on sale; archive the rest"
#  From Rugby Australia (news@rugby.example.org) or Australian Grand Prix (info@grandprix.example.org +1 more): if it's about
#  tickets or a ballot going on sale → alert (Needs attention); otherwise → archive. Gemma will read every email
#  from these senders.
#  Save this rule? [y/N]
emaild rule add "Anything from <organisation> about the canteen roster goes to Needs attention"
emaild rule add "Always archive Strava emails" --yes
emaild rule add "Never archive anything from my accountant"
emaild rule add "guidance: I care less about conference marketing unless I'm speaking"
emaild rules                          # list: status, how often each fired, read-back
emaild rule show rugby                # an id or a few words; shows your words and version history
emaild rule off rugby --until February
emaild rule on 3 · emaild rule rm 3 · emaild rule edit 3 "new wording"
emaild rule apply --days 14           # re-check open decisions now (saving a rule already does the last 30 days)
```

The same works from Telegram (`/rule …` with ✅ Save / ✖ Cancel, `/rules`, `/rule off 3 until February`, or just *"turn off the rugby rule until February"*), MCP (`create_rule` → `confirm_rule`) and the web Rules page.

How rules run:
- Security (spam, phishing, impersonation) and one-time codes always come first; a rule can't let them through.
- A rule without a condition (*always archive Strava*) decides on its own; no model call.
- A rule with a condition (*about tickets going on sale*) skips the bulk-mail shortcut: Gemma reads every email from those senders and judges only the condition; emAIl applies your then/otherwise.
- *Never archive* applies last, after everything else.
- A rule never archives a personal email from a real person unless it names that sender's address or domain; that goes to Review instead.
- Guidance goes into Gemma's instructions for every email.
- Every decision lists the rules that fired (`emaild explain "<subject words>"`, MCP `explain`). Reviewed decisions are never changed by a rule.
- Editing a rule makes a new version that is off until you save it again.

### Dry runs and suggested rules (slice 2)

Apply migration 013 first: `emaild migrate`.

Before you save a rule, its read-back shows what it would have done to the last 30 days of mail. You can also test any rule, or a new wording, without saving anything:

```bash
emaild rule add "Always archive emails from Acme Streaming"
#  [12] Acme Streaming  (rule, waiting for you to confirm, v1, fired 0x)
#       From Acme Streaming (offers@acme.example.com) → archive.
#       In the last 30 days this rule matches 41 emails: 23 would be archived (currently 18 kept, 5 already
#       archived). 2 security-flagged — left alone (security and one-time checks come first).
#         · 2026-10-03  Acme Streaming — New this week: keep → archive
#  Save this rule? [y/N]
emaild rule test 12                   # an existing rule (id or a few words)
emaild rule test "Anything from Riverside Rovers about the canteen roster goes to Needs attention" --days 60
```

- Counts compare the rule with each email's current verdict (your correction wins over emAIl's proposal). Emails you reviewed yourself and handled differently are flagged ("you reviewed 2 of these yourself and chose differently").
- Spam, phishing, one-time codes and copies of an email decided in another mailbox are listed as left alone: rules never change them.
- A rule with a condition needs the model. emAIl checks a small sample of the newest matching emails (8 for `rule test`, 5 in a read-back, 20 at most, set with `--sample`) and estimates the rest ("about 8 of 30 would be alerted (estimated from 8 checked)").
- Telegram: `/rule test 12`; MCP: `dry_run_rule`; web: the Test button on each rule.

emAIl also suggests rules from emails you've reviewed. When you keep handling a sender the same way, and emAIl got it wrong at least once, it proposes a rule:

```bash
emaild rule suggest
#  [3] Acme Streaming: “Always archive emails from offers@acme.example.com”
#       From offers@acme.example.com → archive.
#       why: you archived 6 of 6 emails from offers@acme.example.com; emAIl proposed keep on 3 of them
emaild rule suggest --accept 3        # creates the rule and turns it on
emaild rule suggest --dismiss 3       # never suggest it again
```

Suggestions work per address, or per organisation domain when two or more of its addresses agree (never for free-mail domains). Senders with security, spam or one-time verdicts are never suggested. Suggestions refresh at most once a day and need no model. They show on the web Rules page (Save / Not now / Never), in Telegram (`/suggestrules`, ✅ Save / ✖ Never), in MCP (`rule_suggestions`, `accept_rule_suggestion`, `dismiss_rule_suggestion`), and as a 💡 line in the morning brief.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check-llm` can't connect | Check `curl localhost:11434/api/tags` in WSL. Containers use the same network, so if this works from WSL it works from the containers. |
| `init-db` hangs at "database not ready" | The DB is still initialising. Watch `podman logs emaild_db_1`. |
| Pulling the DB image is denied | Run `podman login container-registry.oracle.com` once, if your podman needs it. |
| ORA-40284 or model errors when searching | `load-model` hasn't been run, or the `.onnx` file isn't in `models/`. |
| Account shows `reauth` | The token was revoked or expired (Testing mode?). Link the account again. |
| Search returns nothing right after linking | The backfill is still running, and embeddings lag behind the sync. |
| Port 8080 or 8081 already in use in WSL | Change `EMAILD_API_PORT` / `EMAILD_MCP_PORT` in `.env`. |
