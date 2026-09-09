"""Filesystem policy for Skill and reference loading."""

from __future__ import annotations

from pathlib import Path, PurePath

from routivus.skill.errors import SkillSecurityError, SkillContentError
from routivus.skill.models import SkillConfig


def ensure_within(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SkillSecurityError("路径超出 Skill 目录") from exc
    return resolved


def validate_reference_path(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise SkillSecurityError("reference 路径不合法")
    value = raw.strip().replace("\\", "/")
    path = PurePath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillSecurityError("reference 必须是 Skill 目录内的相对路径")
    if not path.parts or path.parts[0].lower() != "references":
        raise SkillSecurityError("reference 只能位于 references/ 目录")
    candidate = ensure_within(root, root.joinpath(*path.parts))
    if not candidate.is_file():
        raise SkillSecurityError("reference 文件不存在")
    if candidate.suffix.lower() not in {".md", ".markdown", ".txt", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}:
        raise SkillSecurityError("不支持的 reference 文件类型")
    return candidate


def check_content_limit(text: str, max_chars: int, label: str) -> None:
    if len(text) > max_chars:
        raise SkillContentError(f"{label} 超过 {max_chars} 字符")
