-- Run by `emaild init-db` as SYS (SYSDBA) inside FREEPDB1.
-- Placeholders {owner_pw}/{app_pw}/{admin_pw} are filled in by the CLI. Each statement ends with a line containing only "/".
-- Statements that fail with "already exists" errors are skipped, so this is safe to re-run.

CREATE USER email_owner IDENTIFIED BY "{owner_pw}" DEFAULT TABLESPACE users QUOTA UNLIMITED ON users
/
GRANT DB_DEVELOPER_ROLE, CTXAPP TO email_owner
/
GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE PROCEDURE, CREATE TRIGGER, CREATE MINING MODEL TO email_owner
/
GRANT EXECUTE ON DBMS_RLS TO email_owner
/
GRANT EXECUTE ON DBMS_VECTOR TO email_owner
/
GRANT EXECUTE ON DBMS_SESSION TO email_owner
/
CREATE OR REPLACE CONTEXT emaild_ctx USING email_owner.ctx_pkg
/
CREATE OR REPLACE DIRECTORY emaild_models AS '/opt/oracle/models'
/
GRANT READ ON DIRECTORY emaild_models TO email_owner
/
-- Runtime user for the app: no object privileges of its own; grants come from the owner's migrations.
CREATE USER email_app IDENTIFIED BY "{app_pw}"
/
GRANT CREATE SESSION TO email_app
/
-- Admin/maintenance user (APEX console later). Bypasses VPD; every admin view of content should be audited.
CREATE USER email_admin IDENTIFIED BY "{admin_pw}"
/
GRANT CREATE SESSION, EXEMPT ACCESS POLICY TO email_admin
/
-- Password changes on re-run (keeps .env authoritative)
ALTER USER email_owner IDENTIFIED BY "{owner_pw}"
/
ALTER USER email_app IDENTIFIED BY "{app_pw}"
/
ALTER USER email_admin IDENTIFIED BY "{admin_pw}"
/
