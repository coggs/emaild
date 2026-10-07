"""Database access. Every borrowed connection runs inside a tenant/user context so VPD scopes it."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import oracledb

from .config import settings

log = logging.getLogger(__name__)

oracledb.defaults.fetch_lobs = False  # CLOBs come back as str

OWNER = "EMAIL_OWNER"
APP = "EMAIL_APP"

_pool: oracledb.ConnectionPool | None = None


@dataclass(frozen=True)
class UserCtx:
    tenant_id: int
    user_id: int
    email: str = ""


def _init_session(conn: oracledb.Connection, requested_tag: str | None) -> None:
    conn.current_schema = OWNER
    # an offset, not the region name 'UTC': python-oracledb thin mode can't decode named time zones (DPY-3022)
    conn.cursor().execute("ALTER SESSION SET TIME_ZONE = '+00:00'")


def wait_for_db(user: str, password: str, attempts: int = 60, delay: float = 5.0, **kw) -> None:
    """The Oracle container takes a few minutes on first start; retry until it answers."""
    s = settings()
    for i in range(attempts):
        try:
            oracledb.connect(user=user, password=password, dsn=s.db_dsn, **kw).close()
            return
        except oracledb.Error as e:
            log.info("database not ready (%s/%s): %s", i + 1, attempts, str(e).splitlines()[0])
            time.sleep(delay)
    raise RuntimeError("database did not become available")


def pool() -> oracledb.ConnectionPool:
    global _pool
    if _pool is None:
        s = settings()
        wait_for_db(APP, s.db_app_password)
        _pool = oracledb.create_pool(user=APP, password=s.db_app_password, dsn=s.db_dsn,
                                     min=1, max=8, increment=1, session_callback=_init_session)
    return _pool


@contextmanager
def user_session(ctx: UserCtx) -> Iterator[oracledb.Connection]:
    """Borrow a connection scoped to one user. Commits on success, rolls back on error, always clears context."""
    conn = pool().acquire()
    try:
        conn.cursor().callproc("ctx_pkg.set_user", [ctx.tenant_id, ctx.user_id])
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.cursor().callproc("ctx_pkg.clear")
        finally:
            pool().release(conn)


@contextmanager
def system_session() -> Iterator[oracledb.Connection]:
    """Unscoped read access for the worker (listing accounts/users). Never write content in this mode."""
    conn = pool().acquire()
    try:
        conn.cursor().callproc("ctx_pkg.set_system")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.cursor().callproc("ctx_pkg.clear")
        finally:
            pool().release(conn)


def owner_connection() -> oracledb.Connection:
    s = settings()
    return oracledb.connect(user=OWNER, password=s.db_owner_password, dsn=s.db_dsn)


def sys_connection() -> oracledb.Connection:
    s = settings()
    wait_for_db("SYS", s.oracle_pwd, mode=oracledb.AUTH_MODE_SYSDBA)
    return oracledb.connect(user="SYS", password=s.oracle_pwd, dsn=s.db_dsn, mode=oracledb.AUTH_MODE_SYSDBA)


# ---------- SQL scripts ----------

def split_script(text: str) -> list[str]:
    """Split a script on lines containing only '/'. Full-line '--' comments are dropped."""
    statements, buf = [], []
    for line in text.splitlines():
        if line.strip() == "/":
            stmt = "\n".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        elif line.lstrip().startswith("--") and not buf:
            continue
        else:
            buf.append(line)
    tail = "\n".join(l for l in buf if not l.lstrip().startswith("--")).strip()
    if tail:
        statements.append(tail)
    return statements


IGNORABLE = {955, 1920, 1918, 1430, 1408, 2260, 2261, 2275, 1927}  # already exists / not granted


def run_script(conn: oracledb.Connection, path: Path, params: dict[str, str] | None = None,
               ignore_existing: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    if params:
        for k, v in params.items():
            if '"' in v:
                raise ValueError(f"value for {k} must not contain double quotes")
            text = text.replace("{" + k + "}", v)
    cur = conn.cursor()
    for stmt in split_script(text):
        try:
            cur.execute(stmt)
        except oracledb.DatabaseError as e:
            (err,) = e.args
            if ignore_existing and getattr(err, "code", None) in IGNORABLE:
                log.info("skipped (exists): %s", stmt.splitlines()[0][:80])
                continue
            raise RuntimeError(f"{path.name}: failed on statement:\n{stmt[:400]}\n{e}") from e
    conn.commit()
