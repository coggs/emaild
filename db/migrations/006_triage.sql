-- Phase 1a: triage decisions (shadow mode) and per-sender engagement stats.

CREATE TABLE decisions (
  id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  item_id        NUMBER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  source         VARCHAR2(20) NOT NULL,              -- heuristic | llm
  model          VARCHAR2(200),
  importance     VARCHAR2(10) NOT NULL,              -- high | normal | low
  category       VARCHAR2(30) NOT NULL,
  needs_reply    BOOLEAN DEFAULT FALSE NOT NULL,
  action         VARCHAR2(10) NOT NULL,              -- alert | keep | archive
  confidence     NUMBER(4,3) NOT NULL,
  summary        VARCHAR2(500 CHAR),
  reasons        VARCHAR2(1000 CHAR),
  examples       JSON,                               -- decision ids used as few-shot examples
  needs_review   BOOLEAN DEFAULT FALSE NOT NULL,
  status         VARCHAR2(12) DEFAULT 'proposed' NOT NULL,  -- proposed | approved | rejected | corrected
  corrected      JSON,                               -- {"action":..,"importance":..,"category":..,"needs_reply":..}
  verdict_reason VARCHAR2(1000 CHAR),
  verdict_at     TIMESTAMP WITH TIME ZONE,
  latency_ms     NUMBER,
  created_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT decisions_item_uk UNIQUE (item_id)
)
/
CREATE INDEX decisions_review_ix ON decisions (user_id, needs_review, status)
/
CREATE TABLE sender_stats (
  id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  sender_addr      VARCHAR2(320) NOT NULL,
  received         NUMBER DEFAULT 0 NOT NULL,
  replied          NUMBER DEFAULT 0 NOT NULL,          -- my replies to their messages
  sent_to          NUMBER DEFAULT 0 NOT NULL,          -- messages I sent to them (to/cc)
  avg_reply_hours  NUMBER,
  last_received    TIMESTAMP WITH TIME ZONE,
  last_contact     TIMESTAMP WITH TIME ZONE,          -- last time I wrote to them
  refreshed_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT sender_stats_uk UNIQUE (user_id, sender_addr)
)
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list('DECISIONS','SENDER_STATS'))) LOOP
    DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => t.tname, policy_name => 'USER_SCOPE',
                        function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                        statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                        policy_type => DBMS_RLS.DYNAMIC);
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.tname || ' TO email_app';
  END LOOP;
END;
/
