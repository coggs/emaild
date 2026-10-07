-- Session context + Virtual Private Database policies.
-- The app calls ctx_pkg.set_user(tenant, user) on every borrowed connection; VPD then scopes every query.
-- With no context set, content tables return nothing (1=0). ctx_pkg.set_system is used only by the worker to list accounts.

CREATE OR REPLACE PACKAGE ctx_pkg AUTHID DEFINER AS
  PROCEDURE set_user(p_tenant_id IN NUMBER, p_user_id IN NUMBER);
  PROCEDURE set_system;
  PROCEDURE clear;
END ctx_pkg;
/
CREATE OR REPLACE PACKAGE BODY ctx_pkg AS
  PROCEDURE set_user(p_tenant_id IN NUMBER, p_user_id IN NUMBER) IS
  BEGIN
    DBMS_SESSION.CLEAR_ALL_CONTEXT('EMAILD_CTX');
    DBMS_SESSION.SET_CONTEXT('EMAILD_CTX', 'TENANT_ID', TO_CHAR(p_tenant_id));
    DBMS_SESSION.SET_CONTEXT('EMAILD_CTX', 'USER_ID',   TO_CHAR(p_user_id));
    DBMS_SESSION.SET_CONTEXT('EMAILD_CTX', 'ROLE',      'USER');
  END;
  PROCEDURE set_system IS
  BEGIN
    DBMS_SESSION.CLEAR_ALL_CONTEXT('EMAILD_CTX');
    DBMS_SESSION.SET_CONTEXT('EMAILD_CTX', 'ROLE', 'SYSTEM');
  END;
  PROCEDURE clear IS
  BEGIN
    DBMS_SESSION.CLEAR_ALL_CONTEXT('EMAILD_CTX');
  END;
END ctx_pkg;
/
CREATE OR REPLACE FUNCTION vpd_user_scope(p_schema IN VARCHAR2, p_object IN VARCHAR2) RETURN VARCHAR2 AS
BEGIN
  IF SYS_CONTEXT('EMAILD_CTX', 'ROLE') = 'SYSTEM' THEN
    RETURN NULL;
  ELSIF SYS_CONTEXT('EMAILD_CTX', 'USER_ID') IS NULL THEN
    RETURN '1=0';
  END IF;
  RETURN 'tenant_id = TO_NUMBER(SYS_CONTEXT(''EMAILD_CTX'',''TENANT_ID'')) AND user_id = TO_NUMBER(SYS_CONTEXT(''EMAILD_CTX'',''USER_ID''))';
END;
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list(
              'ACCOUNTS','THREADS','ITEMS','CHUNKS','AUDIT_LOG','LLM_CALLS'))) LOOP
    DBMS_RLS.ADD_POLICY(
      object_schema   => USER,
      object_name     => t.tname,
      policy_name     => 'USER_SCOPE',
      function_schema => USER,
      policy_function => 'VPD_USER_SCOPE',
      statement_types => 'SELECT,INSERT,UPDATE,DELETE',
      update_check    => TRUE,
      policy_type     => DBMS_RLS.DYNAMIC);
  END LOOP;
END;
/
