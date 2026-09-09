from __future__ import annotations

import pytest

from routivus.skill.models import SkillConfig
from routivus.skill.registry import SkillRegistry
from routivus.tool.builtin import build_registry


def make_registry(tmp_path, enabled=True):
    path = tmp_path / ".xg" / "skills" / "demo"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("<!-- xg-skill: name=demo -->\n只读规范", encoding="utf-8")
    return SkillRegistry(
        project_root=tmp_path, config=SkillConfig(enabled=enabled), builtin_root=tmp_path / "none"
    )


@pytest.mark.asyncio
async def test_load_skill_is_registered_as_read_only_builtin_tool(tmp_path):
    registry = make_registry(tmp_path)
    tools = build_registry(base_dir=tmp_path, skill_registry=registry)
    tool = tools.get("load_skill")
    assert tool is not None
    assert tool.source == "builtin-skill"
    assert tools.schemas()[-1]["name"] == "load_skill"

    result = await tools.aexecute("load_skill", {"name": "demo"})
    assert result.ok is True
    assert "只读规范" in result.output


def test_load_skill_is_not_registered_when_disabled(tmp_path):
    registry = make_registry(tmp_path, enabled=False)
    assert "load_skill" not in build_registry(base_dir=tmp_path, skill_registry=registry).names()
