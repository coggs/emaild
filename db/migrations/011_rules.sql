-- Phase 2: rules in plain language. Original wording + compiled JSON, versioned; decisions cite the rules that fired.
CREATE TABLE rules (
  id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  name           VARCHAR2(200 CHAR) NOT NULL,
  kind           VARCHAR2(10) DEFAULT 'rule' NOT NULL CHECK (kind IN ('rule','guidance')),
  original_text  VARCHAR2(2000 CHAR) NOT NULL,       -- the user's own words
  compiled       JSON NOT NULL,                      -- {"match","condition","then","else","floor","read_with_model"}
  readback       VARCHAR2(2000 CHAR),                -- plain English generated from `compiled` (what the user confirmed)
  status         VARCHAR2(10) DEFAULT 'pending' NOT NULL
                 CHECK (status IN ('pending','active','paused','deleted')),
  priority       NUMBER DEFAULT 100 NOT NULL,        -- lower runs first; the first rule with an action decides
  version        NUMBER DEFAULT 1 NOT NULL,
  paused_until   TIMESTAMP WITH TIME ZONE,           -- paused with an end date: applies again after this
  fire_count     NUMBER DEFAULT 0 NOT NULL,
  last_fired_at  TIMESTAMP WITH TIME ZONE,
  created_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE INDEX rules_status_ix ON rules (user_id, status)
/
CREATE TABLE rule_versions (
  id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  rule_id        NUMBER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  version        NUMBER NOT NULL,
  original_text  VARCHAR2(2000 CHAR) NOT NULL,
  compiled       JSON NOT NULL,
  readback       VARCHAR2(2000 CHAR),
  created_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT rule_versions_uk UNIQUE (rule_id, version)
)
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list('RULES','RULE_VERSIONS'))) LOOP
    DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => t.tname, policy_name => 'USER_SCOPE',
                        function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                        statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                        policy_type => DBMS_RLS.DYNAMIC);
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.tname || ' TO email_app';
  END LOOP;
END;
/
-- Which rules fired for a decision (JSON array of rule ids, the deciding rule first).
ALTER TABLE decisions ADD (rule_ids JSON)
/
