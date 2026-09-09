"""Public data models for the Skill system."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SkillConfig:
    enabled: bool = True
    default_enabled: bool = True
    max_index_items: int = 20
    max_index_chars: int = 4_096
    max_skill_chars: int = 32_000
    max_reference_chars: int = 16_000
    max_loaded_chars: int = 64_000
    max_references: int = 8


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    source: str
    root: Path
    version: str | None = None
    enabled: bool = True
    valid: bool = True
    error: str = ""


@dataclass(frozen=True)
class SkillReference:
    path: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class SkillDocument:
    info: SkillInfo
    body: str
    references: tuple[SkillReference, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class SkillLoadRequest:
    name: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillRoot:
    source: str
    path: Path
