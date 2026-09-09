"""Three-layer Skill discovery and precedence handling."""

from __future__ import annotations

from pathlib import Path

from routivus.skill.errors import SkillParseError
from routivus.skill.models import SkillConfig, SkillInfo, SkillRoot
from routivus.skill.parser import NAME_RE, read_metadata


class SkillLoader:
    def __init__(self, roots: tuple[SkillRoot, ...], config: SkillConfig) -> None:
        self.roots = roots
        self.config = config

    def discover(self, overrides: dict[str, bool] | None = None) -> tuple[SkillInfo, ...]:
        overrides = overrides or {}
        selected: dict[str, SkillInfo] = {}
        # Low-to-high order makes project definitions replace user/builtin.
        for skill_root in self.roots:
            directory = skill_root.path
            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name.lower()) if directory.is_dir() else []
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_dir() or not NAME_RE.fullmatch(entry.name):
                    continue
                path = entry / "SKILL.md"
                if not path.is_file():
                    continue
                try:
                    data = read_metadata(entry, name=entry.name, source=skill_root.source)
                    info = SkillInfo(**data, enabled=overrides.get(entry.name, self.config.default_enabled))
                except Exception as exc:
                    info = SkillInfo(
                        name=entry.name, description="无效 Skill", source=skill_root.source,
                        root=entry, enabled=False, valid=False, error=str(exc),
                    )
                selected[entry.name] = info
        return tuple(selected[name] for name in sorted(selected))
