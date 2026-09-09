"""Public models for user input history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryConfig:
    enabled: bool = True
    persist: bool = True
    max_entries: int = 100
    max_entry_chars: int = 8_000
    max_file_bytes: int = 1_048_576


@dataclass(frozen=True)
class HistoryEntry:
    text: str
    created_at: str
    scope: str
    persisted: bool = False


@dataclass
class HistoryCursor:
    index: int | None = None
    draft: str = ""
