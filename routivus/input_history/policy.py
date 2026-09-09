"""Privacy and bounded-input policy for local history."""

from __future__ import annotations

import re


_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|cookie)\b"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_SECRET_PREFIX = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{12,})\b")


def normalize_text(text: str) -> str:
    """Normalize the same whitespace that the CLI submits to the agent."""
    return str(text).strip()


def is_sensitive(text: str) -> bool:
    """Best-effort detection; callers must still offer a persistence switch."""
    value = normalize_text(text)
    if not value:
        return False
    command = value.lower().split(maxsplit=1)[0]
    if command in {"/save", "/config"} and value.lower().startswith(("/save", "/config set")):
        return True
    return bool(_SENSITIVE_ASSIGNMENT.search(value) or _BEARER.search(value) or _SECRET_PREFIX.search(value))


def should_record(text: str) -> bool:
    """Exclude control commands which are not ordinary user tasks."""
    value = normalize_text(text)
    if not value:
        return False
    command = value.lower().split(maxsplit=1)[0]
    return command not in {"/cancel", "/c", "/exit", "/quit", "/history"}
