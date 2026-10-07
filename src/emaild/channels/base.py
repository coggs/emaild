"""The Channel interface every connector implements (email now, instant messaging later)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Item


@dataclass
class Page:
    ids: list[str]
    next_token: str | None


@dataclass
class Changes:
    added: list[str] = field(default_factory=list)
    labels: dict[str, list[str]] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    next_token: str | None = None
    cursor: str | None = None


class CursorExpired(Exception):
    """The provider can no longer give incremental changes from the stored cursor; a rescan is needed."""


class Channel(Protocol):
    provider: str

    def identity(self) -> tuple[str, str]:
        """Return (address, current cursor)."""

    def list_page(self, since_days: int, page_token: str | None) -> Page: ...

    def fetch(self, provider_id: str) -> Item: ...

    def changes(self, cursor: str, page_token: str | None) -> Changes: ...

    def updated_credentials(self) -> dict | None:
        """Credentials to persist if they were refreshed during this session, else None."""
