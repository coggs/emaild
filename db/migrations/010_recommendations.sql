-- Phase 1b recommendations: unsubscribe suggestions (and what the user did with them), and follow-up nudges.
CREATE TABLE unsubscribes (
  id           NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  sender_addr  VARCHAR2(320) NOT NULL,                -- lower-cased
  list_id      VARCHAR2(1000 CHAR),
  method       VARCHAR2(10) CHECK (method IN ('one_click','mailto','url')),
  target       VARCHAR2(4000),                        -- https URL (one-click POST / page to open) or mailto:
  status       VARCHAR2(10) DEFAULT 'suggested' NOT NULL
               CHECK (status IN ('suggested','done','failed','dismissed','manual')),
  detail       VARCHAR2(1000 CHAR),
  created_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  acted_at     TIMESTAMP WITH TIME ZONE,
  CONSTRAINT unsubscribes_uk UNIQUE (user_id, sender_addr)
)
/
BEGIN
  DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => 'UNSUBSCRIBES', policy_name => 'USER_SCOPE',
                      function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                      statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                      policy_type => DBMS_RLS.DYNAMIC);
END;
/
GRANT SELECT, INSERT, UPDATE, DELETE ON unsubscribes TO email_app
/
-- Follow-up nudges ("waiting on others"): dismissing one stamps the sent email itself (items is already VPD-scoped).
ALTER TABLE items ADD (nudge_dismissed_at TIMESTAMP WITH TIME ZONE)
/
