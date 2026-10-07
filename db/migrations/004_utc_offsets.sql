-- Phase 0 fix: early sessions used TIME_ZONE='UTC' (a named region). python-oracledb thin mode can't
-- decode named regions (DPY-3022), so re-store every TIMESTAMP WITH TIME ZONE value with a +00:00 offset.
-- Runs in SYSTEM context so the VPD policies let the owner see every row.
BEGIN
  ctx_pkg.set_system;
  FOR c IN (SELECT table_name, column_name FROM user_tab_columns
             WHERE data_type LIKE 'TIMESTAMP%WITH TIME ZONE'
               AND data_type NOT LIKE '%LOCAL%'
               AND table_name IN ('TENANTS','USERS','ACCOUNTS','THREADS','ITEMS','CHUNKS',
                                  'AUDIT_LOG','LLM_CALLS','EMBEDDING_MODELS','SCHEMA_MIGRATIONS')) LOOP
    EXECUTE IMMEDIATE 'UPDATE ' || c.table_name || ' SET ' || c.column_name ||
      ' = FROM_TZ(CAST(SYS_EXTRACT_UTC(' || c.column_name || ') AS TIMESTAMP), ''+00:00'')' ||
      ' WHERE ' || c.column_name || ' IS NOT NULL';
  END LOOP;
  ctx_pkg.clear;
END;
/
