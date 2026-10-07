-- One-time codes and sign-in links: expiry tracking, and scrubbing emAIl's own copy once expired.
ALTER TABLE decisions ADD (expires_at TIMESTAMP WITH TIME ZONE)
/
ALTER TABLE items ADD (scrubbed_at TIMESTAMP WITH TIME ZONE)
/
CREATE INDEX decisions_expiry_ix ON decisions (user_id, category, expires_at)
/
-- Protected identities: names (people or organisations) that may only send from listed addresses/domains.
CREATE TABLE protected_identities (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  kind        VARCHAR2(10) DEFAULT 'person' NOT NULL CHECK (kind IN ('person','org')),
  name        VARCHAR2(200 CHAR) NOT NULL,
  allowed     JSON NOT NULL,                     -- ["president@riversiderovers.example.org", "@riversiderovers.example.org"]
  note        VARCHAR2(500 CHAR),
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT protected_identities_uk UNIQUE (user_id, name)
)
/
BEGIN
  DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => 'PROTECTED_IDENTITIES', policy_name => 'USER_SCOPE',
                      function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                      statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                      policy_type => DBMS_RLS.DYNAMIC);
END;
/
GRANT SELECT, INSERT, UPDATE, DELETE ON protected_identities TO email_app
/
