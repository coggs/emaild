from __future__ import annotations

from . import db
from .config import settings


def resolve(email: str | None = None) -> db.UserCtx:
    email = (email or settings().default_user).lower()
    if not email:
        raise RuntimeError("no user given and EMAILD_DEFAULT_USER is not set")
    with db.pool().acquire() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, tenant_id FROM users WHERE email = :1", [email])
        r = cur.fetchone()
    if not r:
        raise RuntimeError(f"unknown user {email}; run `emaild init-db` or `emaild create-user`")
    return db.UserCtx(tenant_id=r[1], user_id=r[0], email=email)
