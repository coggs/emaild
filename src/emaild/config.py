"""Settings, read once from environment variables (see .env.example)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret(name: str, file_default: str) -> str:
    """A secret from the env var, else from the file named by <name>_FILE (mounted from secrets/)."""
    v = _env(name)
    if v:
        return v
    path = Path(_env(f"{name}_FILE", file_default))
    try:
        return path.read_text().strip() if path.is_file() else ""
    except OSError:
        return ""


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


def sql_identifier(value: str) -> str:
    """Validate a value that must be spliced into SQL as an identifier (e.g. a mining model name)."""
    if not _IDENT.match(value):
        raise ValueError(f"not a valid SQL identifier: {value!r}")
    return value.upper()


@dataclass(frozen=True)
class Settings:
    # database
    db_dsn: str = field(default_factory=lambda: _env("EMAILD_DB_DSN", "localhost:1521/FREEPDB1"))
    oracle_pwd: str = field(default_factory=lambda: _env("ORACLE_PWD"))
    db_owner_password: str = field(default_factory=lambda: _env("EMAILD_DB_OWNER_PASSWORD"))
    db_app_password: str = field(default_factory=lambda: _env("EMAILD_DB_APP_PASSWORD"))
    db_admin_password: str = field(default_factory=lambda: _env("EMAILD_DB_ADMIN_PASSWORD"))
    db_dir: Path = field(default_factory=lambda: Path(_env("EMAILD_DB_DIR", str(Path(__file__).resolve().parents[2] / "db"))))

    # crypto
    master_key: str = field(default_factory=lambda: _env("EMAILD_MASTER_KEY"))

    # first tenant/user
    tenant_name: str = field(default_factory=lambda: _env("EMAILD_TENANT_NAME", "Default"))
    default_user: str = field(default_factory=lambda: _env("EMAILD_DEFAULT_USER").lower())
    default_user_name: str = field(default_factory=lambda: _env("EMAILD_DEFAULT_USER_NAME"))

    # gmail
    google_client_file: str = field(default_factory=lambda: _env("EMAILD_GOOGLE_CLIENT_FILE", "/run/emaild/google_client.json"))
    public_url: str = field(default_factory=lambda: _env("EMAILD_PUBLIC_URL", "http://localhost:8080").rstrip("/"))
    backfill_days: int = field(default_factory=lambda: _int("EMAILD_BACKFILL_DAYS", 180))
    poll_seconds: int = field(default_factory=lambda: _int("EMAILD_POLL_SECONDS", 90))

    # outlook.com (Microsoft Graph); redirect URI is {public_url}/oauth/microsoft/callback
    ms_client_id: str = field(default_factory=lambda: _env("EMAILD_MS_CLIENT_ID"))
    ms_client_secret: str = field(default_factory=lambda: _secret("EMAILD_MS_CLIENT_SECRET", "/run/emaild/ms_client_secret"),
                                  repr=False)
    ms_tenant: str = field(default_factory=lambda: _env("EMAILD_MS_TENANT", "consumers"))
    # work or school (Microsoft 365 / Entra ID) accounts: 'organizations' = any directory; a tenant GUID or verified
    # domain locks linking to that one directory
    ms_work_tenant: str = field(default_factory=lambda: _env("EMAILD_MS_WORK_TENANT", "organizations"))

    # storage
    blob_dir: Path = field(default_factory=lambda: Path(_env("EMAILD_BLOB_DIR", "/data/blobs")))

    # embeddings
    embed_model: str = field(default_factory=lambda: sql_identifier(_env("EMAILD_EMBED_MODEL", "ALL_MINILM_L12_V2")))
    embed_file: str = field(default_factory=lambda: _env("EMAILD_EMBED_FILE", "all_MiniLM_L12_v2.onnx"))
    embed_batch: int = field(default_factory=lambda: _int("EMAILD_EMBED_BATCH", 200))

    # llm
    llm_provider: str = field(default_factory=lambda: _env("EMAILD_LLM_PROVIDER", "ollama"))
    ollama_url: str = field(default_factory=lambda: _env("EMAILD_OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    llm_model: str = field(default_factory=lambda: _env("EMAILD_LLM_MODEL", "gemma4"))
    llm_num_ctx: int = field(default_factory=lambda: _int("EMAILD_LLM_NUM_CTX", 16384))
    openai_base_url: str = field(default_factory=lambda: _env("EMAILD_OPENAI_BASE_URL").rstrip("/"))
    openai_api_key: str = field(default_factory=lambda: _env("EMAILD_OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("EMAILD_OPENAI_MODEL"))
    privacy_default: str = field(default_factory=lambda: _env("EMAILD_PRIVACY_DEFAULT", "local_only"))

    # triage (phase 1a)
    triage_days: int = field(default_factory=lambda: _int("EMAILD_TRIAGE_DAYS", 14))
    triage_per_cycle: int = field(default_factory=lambda: _int("EMAILD_TRIAGE_PER_CYCLE", 25))
    review_threshold: float = field(default_factory=lambda: float(_env("EMAILD_REVIEW_THRESHOLD", "0.75")))
    triage_model: str = field(default_factory=lambda: _env("EMAILD_TRIAGE_MODEL"))  # defaults to EMAILD_LLM_MODEL
    spot_check_rate: float = field(default_factory=lambda: float(_env("EMAILD_SPOT_CHECK_RATE", "0.05")))

    # brief + telegram (phase 1b/1c)
    timezone: str = field(default_factory=lambda: _env("EMAILD_TZ", "Australia/Sydney"))
    brief_time: str = field(default_factory=lambda: _env("EMAILD_BRIEF_TIME", "08:00"))
    brief_days: str = field(default_factory=lambda: _env("EMAILD_BRIEF_DAYS", "mon,tue,wed,thu,fri,sat,sun"))
    brief_llm: bool = field(default_factory=lambda: _env("EMAILD_BRIEF_LLM", "1") not in ("0", "false", "no"))
    telegram_token: str = field(default_factory=lambda: _env("EMAILD_TELEGRAM_TOKEN"))
    quiet_hours: str = field(default_factory=lambda: _env("EMAILD_QUIET_HOURS", "22:00-07:00"))
    telegram_show_codes: bool = field(default_factory=lambda: _env("EMAILD_TELEGRAM_SHOW_CODES", "0") in ("1", "true", "yes"))
    code_default_minutes: int = field(default_factory=lambda: _int("EMAILD_CODE_DEFAULT_MINUTES", 15))

    # mcp
    mcp_token: str = field(default_factory=lambda: _env("EMAILD_MCP_TOKEN"))
    mcp_port: int = field(default_factory=lambda: _int("EMAILD_MCP_PORT", 8081))
    api_port: int = field(default_factory=lambda: _int("EMAILD_API_PORT", 8080))
    bind_host: str = field(default_factory=lambda: _env("EMAILD_BIND_HOST", "127.0.0.1"))
    web_password: str = field(default_factory=lambda: _env("EMAILD_WEB_PASSWORD"))


@lru_cache
def settings() -> Settings:
    return Settings()
