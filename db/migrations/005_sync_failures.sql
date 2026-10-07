-- Track messages that failed to fetch/parse so they are retried (up to 3 attempts) instead of silently skipped.
CREATE TABLE sync_failures (
  id           NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  account_id   NUMBER NOT NULL REFERENCES accounts(id),
  provider_id  VARCHAR2(200) NOT NULL,
  attempts     NUMBER DEFAULT 1 NOT NULL,
  last_error   VARCHAR2(2000),
  last_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT sync_failures_uk UNIQUE (account_id, provider_id)
)
/
BEGIN
  DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => 'SYNC_FAILURES', policy_name => 'USER_SCOPE',
                      function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                      statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                      policy_type => DBMS_RLS.DYNAMIC);
END;
/
GRANT SELECT, INSERT, UPDATE, DELETE ON sync_failures TO email_app
/
-- Character (not byte) length semantics, so long non-ASCII subjects/names don't fail inserts with ORA-12899.
ALTER TABLE items MODIFY (subject VARCHAR2(1000 CHAR), sender_name VARCHAR2(500 CHAR), snippet VARCHAR2(1000 CHAR),
                          rfc_message_id VARCHAR2(1000 CHAR), in_reply_to VARCHAR2(1000 CHAR))
/
ALTER TABLE threads MODIFY (subject VARCHAR2(1000 CHAR))
/
