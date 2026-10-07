"""Bootstrap users (as SYS) and apply numbered migrations (as the schema owner)."""
from __future__ import annotations

import logging

import oracledb

from . import db
from .config import settings

log = logging.getLogger(__name__)


def bootstrap() -> None:
    s = settings()
    for name in ("oracle_pwd", "db_owner_password", "db_app_password", "db_admin_password"):
        if not getattr(s, name):
            raise RuntimeError(f"{name.upper()} is not set")
    with db.sys_connection() as conn:
        for path in sorted((s.db_dir / "bootstrap").glob("*.sql")):
            log.info("bootstrap %s", path.name)
            db.run_script(conn, path, {"owner_pw": s.db_owner_password, "app_pw": s.db_app_password,
                                       "admin_pw": s.db_admin_password}, ignore_existing=True)


def migrate() -> list[str]:
    s = settings()
    applied: list[str] = []
    with db.owner_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("CREATE TABLE schema_migrations (name VARCHAR2(200) PRIMARY KEY, "
                        "applied_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP)")
        except oracledb.DatabaseError as e:
            if e.args[0].code != 955:
                raise
        cur.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in cur}
        for path in sorted((s.db_dir / "migrations").glob("*.sql")):
            if path.name in done:
                continue
            log.info("migrate %s", path.name)
            db.run_script(conn, path)
            cur.execute("INSERT INTO schema_migrations (name) VALUES (:1)", [path.name])
            conn.commit()
            applied.append(path.name)
    return applied


def ensure_tenant_user(tenant_name: str, email: str, display_name: str = "", role: str = "owner") -> db.UserCtx:
    """Create (or find) a tenant and user. Uses the owner connection; tenants/users are not VPD-scoped."""
    email = email.lower()
    with db.owner_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, tenant_id FROM users WHERE email = :1", [email])
        row = cur.fetchone()
        if row:
            return db.UserCtx(tenant_id=row[1], user_id=row[0], email=email)
        cur.execute("SELECT id FROM tenants WHERE name = :1", [tenant_name])
        t = cur.fetchone()
        if t:
            tenant_id = t[0]
        else:
            tid = cur.var(oracledb.NUMBER)
            cur.execute("INSERT INTO tenants (name) VALUES (:1) RETURNING id INTO :2", [tenant_name, tid])
            tenant_id = int(tid.getvalue()[0])
        uid = cur.var(oracledb.NUMBER)
        cur.execute("INSERT INTO users (tenant_id, email, display_name, role) VALUES (:1,:2,:3,:4) RETURNING id INTO :5",
                    [tenant_id, email, display_name or None, role, uid])
        conn.commit()
        return db.UserCtx(tenant_id=tenant_id, user_id=int(uid.getvalue()[0]), email=email)


def load_embedding_model(file_name: str | None = None, model_name: str | None = None, dims: int = 384) -> None:
    """Load an ONNX embedding model from the models/ folder into the DB and make it the active model."""
    s = settings()
    file_name = file_name or s.embed_file
    model_name = model_name or s.embed_model
    with db.owner_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_mining_models WHERE model_name = :1", [model_name])
        if cur.fetchone()[0] == 0:
            log.info("loading %s as %s (this can take a minute)", file_name, model_name)
            cur.callproc("DBMS_VECTOR.LOAD_ONNX_MODEL", keyword_parameters={
                "directory": "EMAILD_MODELS", "file_name": file_name, "model_name": model_name})
        cur.execute(f"GRANT SELECT ON MINING MODEL {model_name} TO email_app")
        cur.execute(f"SELECT VECTOR_DIMENSION_COUNT(VECTOR_EMBEDDING({model_name} USING 'dimension probe' AS data)) FROM dual")
        actual = cur.fetchone()[0]
        if actual != dims:
            raise RuntimeError(f"model produces {actual} dimensions, schema expects {dims}")
        cur.execute("UPDATE embedding_models SET active = FALSE")
        cur.execute("""MERGE INTO embedding_models m USING (SELECT :name AS name FROM dual) s ON (m.name = s.name)
                       WHEN MATCHED THEN UPDATE SET active = TRUE, source_file = :f, dims = :d
                       WHEN NOT MATCHED THEN INSERT (name, source_file, dims, active) VALUES (:name, :f, :d, TRUE)""",
                    {"name": model_name, "f": file_name, "d": dims})
        conn.commit()
