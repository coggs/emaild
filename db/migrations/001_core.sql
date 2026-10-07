-- emAIl core schema. Run as EMAIL_OWNER by `emaild migrate`.
-- Every content table carries tenant_id + user_id, defaulted from the session context so the app cannot forget them.

CREATE TABLE tenants (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        VARCHAR2(200) NOT NULL,
  settings    JSON,
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE TABLE users (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     NUMBER NOT NULL REFERENCES tenants(id),
  email         VARCHAR2(320) NOT NULL,
  display_name  VARCHAR2(200),
  role          VARCHAR2(20) DEFAULT 'member' NOT NULL CHECK (role IN ('owner','admin','member')),
  settings      JSON,
  created_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT users_email_uk UNIQUE (email)
)
/
CREATE TABLE accounts (
  id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL REFERENCES tenants(id),
  user_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL REFERENCES users(id),
  provider         VARCHAR2(30) NOT NULL,            -- gmail | graph | imap
  address          VARCHAR2(320) NOT NULL,
  status           VARCHAR2(20) DEFAULT 'active' NOT NULL,  -- active | paused | error | reauth
  privacy_policy   VARCHAR2(30) DEFAULT 'local_only' NOT NULL,
  token_enc        BLOB,                             -- encrypted OAuth credentials (per-user key)
  sync_cursor      VARCHAR2(100),                    -- Gmail historyId
  backfill_token   VARCHAR2(500),                    -- page token while backfilling
  backfill_done    BOOLEAN DEFAULT FALSE NOT NULL,
  last_sync_at     TIMESTAMP WITH TIME ZONE,
  last_error       VARCHAR2(4000),
  created_at       TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT accounts_uk UNIQUE (user_id, provider, address)
)
/
CREATE TABLE threads (
  id                  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id           NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id             NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  account_id          NUMBER NOT NULL REFERENCES accounts(id),
  provider_thread_id  VARCHAR2(200) NOT NULL,
  subject             VARCHAR2(1000),
  first_at            TIMESTAMP WITH TIME ZONE,
  last_at             TIMESTAMP WITH TIME ZONE,
  message_count       NUMBER DEFAULT 0 NOT NULL,
  CONSTRAINT threads_uk UNIQUE (account_id, provider_thread_id)
)
/
CREATE TABLE items (
  id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  account_id       NUMBER NOT NULL REFERENCES accounts(id),
  thread_id        NUMBER REFERENCES threads(id),
  channel          VARCHAR2(20) DEFAULT 'email' NOT NULL,
  provider_id      VARCHAR2(200) NOT NULL,
  rfc_message_id   VARCHAR2(1000),
  in_reply_to      VARCHAR2(1000),
  sender_addr      VARCHAR2(320),
  sender_name      VARCHAR2(500),
  recipients       JSON,          -- {"to":[...],"cc":[...]}
  subject          VARCHAR2(1000),
  sent_at          TIMESTAMP WITH TIME ZONE,
  received_at      TIMESTAMP WITH TIME ZONE,
  snippet          VARCHAR2(1000),
  body_text        CLOB,          -- cleaned text (quotes/signatures stripped)
  full_text        CLOB,          -- full decoded text (escape hatch, before cleaning)
  labels           JSON,
  meta             JSON,          -- list-unsubscribe, precedence, auto-submitted, ...
  attachments      JSON,          -- [{filename, mime_type, size}]
  is_from_me       BOOLEAN DEFAULT FALSE NOT NULL,
  size_bytes       NUMBER,
  blob_path        VARCHAR2(1000),
  created_at       TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT items_uk UNIQUE (account_id, provider_id)
)
/
CREATE INDEX items_user_date_ix ON items (user_id, received_at DESC)
/
CREATE INDEX items_sender_ix ON items (user_id, sender_addr)
/
CREATE INDEX items_thread_ix ON items (thread_id)
/
CREATE TABLE embedding_models (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        VARCHAR2(128) NOT NULL UNIQUE,   -- mining model name in the DB
  source_file VARCHAR2(500),
  dims        NUMBER NOT NULL,
  active      BOOLEAN DEFAULT FALSE NOT NULL,
  loaded_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE TABLE chunks (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  item_id     NUMBER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  seq         NUMBER NOT NULL,
  content     VARCHAR2(4000) NOT NULL,
  embedding   VECTOR(384, FLOAT32),          -- filled asynchronously by `embed-pending`
  model_id    NUMBER REFERENCES embedding_models(id),
  CONSTRAINT chunks_uk UNIQUE (item_id, seq)
)
/
CREATE INDEX chunks_pending_ix ON chunks (user_id, model_id)
/
CREATE TABLE audit_log (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  actor       VARCHAR2(50) NOT NULL,      -- system | user | mcp | api
  action      VARCHAR2(100) NOT NULL,
  target      VARCHAR2(200),
  detail      JSON
)
/
CREATE TABLE llm_calls (
  id                 NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id            NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  at                 TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  task               VARCHAR2(50) NOT NULL,
  provider           VARCHAR2(50) NOT NULL,
  model              VARCHAR2(200) NOT NULL,
  is_local           BOOLEAN NOT NULL,
  prompt_tokens      NUMBER,
  completion_tokens  NUMBER,
  latency_ms         NUMBER,
  ok                 BOOLEAN NOT NULL,
  error              VARCHAR2(2000)
)
/
