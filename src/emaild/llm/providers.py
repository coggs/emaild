"""LLM providers: Ollama (local) and any OpenAI-compatible endpoint (OCI GenAI, OpenAI, vLLM, LiteLLM...)."""
from __future__ import annotations

import time

import httpx

from .base import LLMResult


class OllamaProvider:
    name = "ollama"
    is_local = True

    def __init__(self, base_url: str, model: str, num_ctx: int = 16384, timeout: float = 300):
        self.base_url, self.model, self.num_ctx, self.timeout = base_url.rstrip("/"), model, num_ctx, timeout

    def models(self) -> list[str]:
        r = httpx.get(f"{self.base_url}/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float = 0.1) -> LLMResult:
        body: dict = {"model": self.model, "messages": messages, "stream": False,
                      "options": {"temperature": temperature, "num_ctx": self.num_ctx}}
        if schema:
            body["format"] = schema  # Ollama structured outputs: JSON schema-constrained decoding
        t0 = time.monotonic()
        r = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return LLMResult(text=data["message"]["content"], model=self.model, provider=self.name, is_local=True,
                         prompt_tokens=data.get("prompt_eval_count"), completion_tokens=data.get("eval_count"),
                         latency_ms=int((time.monotonic() - t0) * 1000))


class OpenAICompatProvider:
    name = "openai_compat"
    is_local = False

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float = 0.1) -> LLMResult:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": schema, "strict": True}}
        t0 = time.monotonic()
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        return LLMResult(text=data["choices"][0]["message"]["content"], model=self.model, provider=self.name,
                         is_local=False, prompt_tokens=usage.get("prompt_tokens"),
                         completion_tokens=usage.get("completion_tokens"),
                         latency_ms=int((time.monotonic() - t0) * 1000))
