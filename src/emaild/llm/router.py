"""Picks a provider per task, enforces the privacy policy, and logs every call."""
from __future__ import annotations

import logging

import oracledb

from ..config import Settings, settings
from .base import LLMProvider, LLMResult, PolicyError
from .providers import OllamaProvider, OpenAICompatProvider

log = logging.getLogger(__name__)


def build_providers(s: Settings) -> dict[str, LLMProvider]:
    providers: dict[str, LLMProvider] = {"ollama": OllamaProvider(s.ollama_url, s.llm_model, s.llm_num_ctx)}
    if s.openai_base_url and s.openai_model:
        providers["openai_compat"] = OpenAICompatProvider(s.openai_base_url, s.openai_api_key, s.openai_model)
    return providers


class Router:
    # Phase 0: every task goes to the default provider. Later: per-task routing table + escalation.
    def __init__(self, s: Settings | None = None, providers: dict[str, LLMProvider] | None = None):
        self.s = s or settings()
        self.providers = providers if providers is not None else build_providers(self.s)

    def pick(self, task: str, policy: str) -> LLMProvider:
        name = self.s.llm_provider
        if name not in self.providers:
            raise RuntimeError(f"LLM provider {name!r} is not configured")
        p = self.providers[name]
        if not p.is_local and policy == "local_only":
            raise PolicyError(f"privacy policy is local_only; refusing to send '{task}' data to {p.name}")
        return p

    def chat(self, task: str, messages: list[dict], *, policy: str = "local_only", schema: dict | None = None,
             conn: oracledb.Connection | None = None, temperature: float = 0.1) -> LLMResult:
        p = self.pick(task, policy)
        try:
            res = p.chat(messages, schema=schema, temperature=temperature)
        except Exception as e:
            _log_call(conn, task, p, None, str(e))
            raise
        _log_call(conn, task, p, res, None)
        return res


def _log_call(conn, task: str, p: LLMProvider, res: LLMResult | None, error: str | None) -> None:
    if conn is None:
        return
    try:
        conn.cursor().execute(
            """INSERT INTO llm_calls (task, provider, model, is_local, prompt_tokens, completion_tokens, latency_ms, ok, error)
               VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9)""",
            [task, p.name, p.model, p.is_local, res.prompt_tokens if res else None,
             res.completion_tokens if res else None, res.latency_ms if res else None, error is None,
             (error or "")[:2000] or None])
    except oracledb.Error:
        log.exception("could not log llm call")
