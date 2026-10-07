"""Raw MIME storage outside the database, encrypted with the owning user's key."""
from __future__ import annotations

from pathlib import Path

from . import crypto
from .config import settings
from .db import UserCtx


def _safe(part: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in part)


def put(ctx: UserCtx, account_id: int, provider_id: str, raw: bytes) -> str:
    s = settings()
    rel = Path(str(ctx.tenant_id), str(ctx.user_id), str(account_id), _safe(provider_id)[:2] or "_",
               _safe(provider_id) + ".eml.enc")
    path = s.blob_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(crypto.encrypt(s.master_key, ctx.tenant_id, ctx.user_id, raw))
    tmp.replace(path)
    return str(rel).replace("\\", "/")


def get(ctx: UserCtx, rel: str) -> bytes:
    s = settings()
    path = (s.blob_dir / rel).resolve()
    if not str(path).startswith(str(s.blob_dir.resolve())):
        raise ValueError("invalid blob path")
    return crypto.decrypt(s.master_key, ctx.tenant_id, ctx.user_id, path.read_bytes())


def delete(ctx: UserCtx, rel: str) -> bool:
    s = settings()
    path = (s.blob_dir / rel).resolve()
    if not str(path).startswith(str(s.blob_dir.resolve())):
        raise ValueError("invalid blob path")
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
