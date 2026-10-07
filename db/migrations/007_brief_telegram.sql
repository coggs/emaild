-- Phase 1b/1c: morning briefs, Telegram chat links, alert notification tracking.

CREATE TABLE briefs (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id       NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  period_start  TIMESTAMP WITH TIME ZONE NOT NULL,
  period_end    TIMESTAMP WITH TIME ZONE NOT NULL,
  kind          VARCHAR2(20) DEFAULT 'morning' NOT NULL,      -- morning | on_demand
  content       JSON NOT NULL,
  delivered_via VARCHAR2(50),
  created_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE INDEX briefs_user_ix ON briefs (user_id, created_at DESC)
/
CREATE TABLE telegram_links (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id       NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  chat_id       NUMBER NOT NULL,
  detail_level  VARCHAR2(10) DEFAULT 'summary' NOT NULL CHECK (detail_level IN ('minimal','summary','full')),
  muted         BOOLEAN DEFAULT FALSE NOT NULL,
  linked_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT telegram_links_user_uk UNIQUE (user_id),
  CONSTRAINT telegram_links_chat_uk UNIQUE (chat_id)
)
/
CREATE TABLE telegram_codes (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id       NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  code          VARCHAR2(12) NOT NULL UNIQUE,
  expires_at    TIMESTAMP WITH TIME ZONE NOT NULL
)
/
ALTER TABLE decisions ADD (notified_at TIMESTAMP WITH TIME ZONE)
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list('BRIEFS','TELEGRAM_LINKS','TELEGRAM_CODES'))) LOOP
    DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => t.tname, policy_name => 'USER_SCOPE',
                        function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                        statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                        policy_type => DBMS_RLS.DYNAMIC);
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.tname || ' TO email_app';
  END LOOP;
END;
/
