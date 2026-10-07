from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    is_local: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0


class LLMProvider(Protocol):
    name: str
    is_local: bool
    model: str

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float = 0.1) -> LLMResult: ...


class PolicyError(RuntimeError):
    """The privacy policy does not allow this provider for this data."""
