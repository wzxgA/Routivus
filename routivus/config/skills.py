"""User/project Skill configuration and environment overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from routivus.skill.models import SkillConfig


SKILLS_CONFIG_FILE = "skills.json"


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class SkillConfigManager:
    def __init__(self, *, user_dir: str | Path | None = None,
                 project_root: str | Path | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.user_dir = Path(user_dir) if user_dir else Path.home() / ".routivus"
        self.project_root = (Path(project_root) if project_root else Path.cwd()).resolve()
        self.user_config_path = self.user_dir / SKILLS_CONFIG_FILE
        self.project_config_path = self.project_root / ".routivus" / SKILLS_CONFIG_FILE
        self.env = env if env is not None else os.environ
        self.errors: list[str] = []

    def _read(self, path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.errors.append(f"{path}: JSON 配置读取失败")
            return {}
        if not isinstance(data, dict):
            self.errors.append(f"{path}: 顶层必须是 JSON 对象")
            return {}
        return data

    @staticmethod
    def _bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() not in {"off", "false", "0", "no"}
        return default

    @staticmethod
    def _int(value: Any, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return default

    def load(self) -> SkillConfig:
        self.errors.clear()
        user = self._read(self.user_config_path)
        project = self._read(self.project_config_path)
        raw = _merge(user, project)
        env = self.env
        return SkillConfig(
            enabled=self._bool(env.get("ROUTIVUS_SKILLS_ENABLED", raw.get("enabled", True)), True),
            default_enabled=self._bool(raw.get("default_enabled", True), True),
            max_index_items=self._int(env.get("ROUTIVUS_SKILLS_MAX_INDEX_ITEMS", raw.get("max_index_items", 20)), 20),
            max_index_chars=self._int(env.get("ROUTIVUS_SKILLS_MAX_INDEX_CHARS", raw.get("max_index_chars", 4_096)), 4_096),
            max_skill_chars=self._int(env.get("ROUTIVUS_SKILLS_MAX_CHARS", raw.get("max_skill_chars", 32_000)), 32_000),
            max_reference_chars=self._int(env.get("ROUTIVUS_SKILLS_MAX_REFERENCE_CHARS", raw.get("max_reference_chars", 16_000)), 16_000),
            max_loaded_chars=self._int(env.get("ROUTIVUS_SKILLS_MAX_LOADED_CHARS", raw.get("max_loaded_chars", 64_000)), 64_000),
            max_references=self._int(raw.get("max_references", 8), 8),
        )

    def enabled_overrides(self) -> dict[str, bool]:
        raw = _merge(self._read(self.user_config_path), self._read(self.project_config_path))
        enabled = raw.get("enabled", {})
        return {str(name): bool(value) for name, value in enabled.items()} if isinstance(enabled, dict) else {}

    def set_enabled(self, name: str, enabled: bool, *, project: bool = True) -> None:
        path = self.project_config_path if project else self.user_config_path
        data = self._read(path)
        mapping = data.setdefault("enabled", {})
        if not isinstance(mapping, dict):
            mapping = {}
            data["enabled"] = mapping
        mapping[name] = bool(enabled)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_skill_config(*, user_dir=None, project_root=None, env=None) -> SkillConfig:
    return SkillConfigManager(user_dir=user_dir, project_root=project_root, env=env).load()
