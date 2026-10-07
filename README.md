# emAIl

An email client where you never see the email. An AI harness owns the inbox; you ask questions, get briefs, and make decisions.

**Status: Phase 1c (triage + brief + Telegram, shadow mode).**
- **Phase 0:** read-only Gmail sync into Oracle Database 26ai, in-database ONNX embeddings, hybrid search, and an MCP server.
- **Phase 1a:** every email gets a proposed decision: *alert*, *keep* or *archive*, plus importance and category. Header/label rules handle obvious bulk mail, and Gemma decides the rest, using your reply history and your past verdicts. You approve or correct the proposals on the web Review page or through MCP. Nothing is done to your mailbox.
- **Phase 1b/1c:** a morning brief (alerts, emails waiting on your reply, things worth knowing, new senders, and how much was filed as noise), a "Needs attention" dashboard, and a Telegram bot that delivers the brief and alerts, takes review verdicts from your phone, and answers questions.

- Design spec: see the EmAIl project doc `emAIl-design-spec.md`
- Setup on WSL2 (Ubuntu) + rootless Podman: [docs/SETUP.md](docs/SETUP.md)

## Layout

```
compose.yaml          db (Oracle 26ai Free) + api (:8080) + worker + mcp (:8081), app containers on host networking
Containerfile         one Python 3.12 image, entrypoint `emaild <command>`
db/bootstrap/         users, context, directory (run as SYS by `emaild init-db`)
db/migrations/        schema, VPD policies, Oracle Text index, grants (run as EMAIL_OWNER)
src/emaild/
  channels/           Channel interface + Gmail connector (gmail.readonly, History API polling)
  normalise.py        MIME parsing, quote/signature stripping, chunking
  store.py            writes/reads (always inside a user-scoped VPD session)
  sync.py             worker loop: backfill -> incremental -> embed backlog
  search.py           Oracle Text + vector similarity, reciprocal rank fusion
  ask.py              cited answers via the LLM router
  llm/                Ollama + OpenAI-compatible providers, privacy-aware router, call logging
  mcp_server.py       MCP tools (stdio or streamable HTTP with bearer token)
  web/                FastAPI + HTMX: status page and Gmail linking
tests/                unit tests (no database needed): `pytest`
```

## Security model (Phase 0)

- Every content row carries `tenant_id`/`user_id`, defaulted from the DB session context; **Oracle VPD** policies scope every query to the current user, and return nothing if no user is set.
- OAuth tokens and raw MIME are encrypted with per-user keys derived from `EMAILD_MASTER_KEY`.
- The app connects as `EMAIL_APP` (DML only). `EMAIL_ADMIN` (exempt from VPD) is reserved for the APEX maintenance console.
- Ports are bound to `127.0.0.1` only. The web app has no sign-in yet (single user) — don't expose it until Phase 7 adds OIDC.
- Email content is treated as untrusted in every prompt; the LLM has no tools in Phase 0.
