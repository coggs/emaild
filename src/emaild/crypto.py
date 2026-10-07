"""Envelope-style encryption: one master key (env), per-user data keys derived with HKDF.

Phase 0 derives user keys deterministically from (master key, tenant, user). Later the master
key can move to OCI Vault and user keys can become random + wrapped, without changing callers.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _master_bytes(master_key: str) -> bytes:
    if not master_key:
        raise RuntimeError("EMAILD_MASTER_KEY is not set (see .env.example)")
    raw = base64.urlsafe_b64decode(master_key.encode())
    if len(raw) < 32:
        raise RuntimeError("EMAILD_MASTER_KEY must decode to at least 32 bytes")
    return raw


def user_fernet(master_key: str, tenant_id: int, user_id: int) -> Fernet:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"emaild-v1",
                info=f"user-data-key:{tenant_id}:{user_id}".encode())
    key = hkdf.derive(_master_bytes(master_key))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(master_key: str, tenant_id: int, user_id: int, data: bytes) -> bytes:
    return user_fernet(master_key, tenant_id, user_id).encrypt(data)


def decrypt(master_key: str, tenant_id: int, user_id: int, token: bytes) -> bytes:
    return user_fernet(master_key, tenant_id, user_id).decrypt(token)
