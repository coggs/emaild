-- Full-text index (Oracle Text) on chunk content, kept in sync on commit.
CREATE INDEX chunks_txt_ix ON chunks (content) INDEXTYPE IS CTXSYS.CONTEXT PARAMETERS ('SYNC (ON COMMIT)')
/
-- Grants for the runtime user. Tenants/users are not VPD-scoped (no mail content), so the app only reads/creates them.
BEGIN
  FOR t IN (SELECT table_name FROM user_tables
             WHERE table_name IN ('ACCOUNTS','THREADS','ITEMS','CHUNKS','AUDIT_LOG','LLM_CALLS')) LOOP
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.table_name || ' TO email_app';
  END LOOP;
  EXECUTE IMMEDIATE 'GRANT SELECT, INSERT ON tenants TO email_app';
  EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE ON users TO email_app';
  EXECUTE IMMEDIATE 'GRANT SELECT ON embedding_models TO email_app';
  EXECUTE IMMEDIATE 'GRANT EXECUTE ON ctx_pkg TO email_app';
END;
/
