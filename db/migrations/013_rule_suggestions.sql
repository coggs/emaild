-- Phase 2 slice 2: rule suggestions mined from the user's reviewed decisions, and what the user did with them.
-- skey is "addr:<address>:<action>" or "domain:<domain>:<action>"; a dismissed key is never suggested again.
CREATE TABLE rule_suggestions (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  skey        VARCHAR2(400) NOT NULL,
  action      VARCHAR2(10) NOT NULL CHECK (action IN ('alert','keep','archive')),
  label       VARCHAR2(200 CHAR),                  -- the sender's display name, for the card
  text        VARCHAR2(2000 CHAR) NOT NULL,         -- ready-made rule text ("Always archive emails from ...")
  evidence    VARCHAR2(1000 CHAR),                  -- "you archived 6 of 6 emails from ...; emAIl proposed keep on 3"
  status      VARCHAR2(10) DEFAULT 'open' NOT NULL CHECK (status IN ('open','accepted','dismissed')),
  rule_id     NUMBER REFERENCES rules(id) ON DELETE SET NULL,   -- the rule created when accepted
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  acted_at    TIMESTAMP WITH TIME ZONE,
  CONSTRAINT rule_suggestions_uk UNIQUE (user_id, skey)
)
/
BEGIN
  DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => 'RULE_SUGGESTIONS', policy_name => 'USER_SCOPE',
                      function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                      statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                      policy_type => DBMS_RLS.DYNAMIC);
END;
/
GRANT SELECT, INSERT, UPDATE, DELETE ON rule_suggestions TO email_app
/
