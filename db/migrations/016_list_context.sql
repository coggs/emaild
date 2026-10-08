-- Numbered lists (/show N), aged-out orders, and per-account Microsoft authority (work or school accounts).
--
-- list_context: every reply that lists emails remembers its item ids in order, so "/show 3" (or replying "3" to
-- that message) opens the right email. One row per list message; ref is "<chat_id>:<message_id>" for Telegram and
-- 'last' for the CLI. The newest row per channel is the "latest list". The app keeps ~50 rows per user per channel.
CREATE TABLE list_context (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  channel     VARCHAR2(10) NOT NULL CHECK (channel IN ('telegram','cli','web','mcp')),
  ref         VARCHAR2(100) NOT NULL,
  item_ids    JSON NOT NULL,                       -- [501, 498, 497]: position n-1 is "[n]"
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE INDEX list_context_latest_ix ON list_context (user_id, channel, created_at)
/
CREATE INDEX list_context_ref_ix ON list_context (user_id, channel, ref)
/
BEGIN
  DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => 'LIST_CONTEXT', policy_name => 'USER_SCOPE',
                      function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                      statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                      policy_type => DBMS_RLS.DYNAMIC);
END;
/
GRANT SELECT, INSERT, UPDATE, DELETE ON list_context TO email_app
/
-- Why a tracker item left the board when it wasn't a confirmed finish: 'assumed_delivered' (an order that went
-- quiet: no update 21 days past its expected date, or 30 days without one) or 'marked' (the user said so).
-- NULL = closed by the normal rules (delivered + 7 days, refunded, ...). A later email reopens assumed ones.
ALTER TABLE tracker_items ADD (closed_reason VARCHAR2(30)
  CONSTRAINT tracker_items_reason_ck CHECK (closed_reason IN ('assumed_delivered','marked')))
/
-- Microsoft identity platform authority per account: NULL = EMAILD_MS_TENANT (personal accounts, 'consumers' by
-- default); a work or school account stores the authority it was linked with ('organizations', a tenant GUID or a
-- verified domain), and its token refreshes go to that same authority.
ALTER TABLE accounts ADD (ms_tenant VARCHAR2(100))
/
