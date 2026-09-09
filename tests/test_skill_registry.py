from __future__ import annotations

import os

import pytest

from routivus.skill.errors import SkillContentError, SkillDisabledError, SkillNotFoundError
from routivus.skill.models import SkillConfig, SkillLoadRequest
from routivus.skill.registry import SkillRegistry


def make_registry(tmp_path, config=None):
    skills = tmp_path / ".routivus" / "skills" / "demo" / "references"
    skills.mkdir(parents=True)
    (skills.parent / "SKILL.md").write_text(
        '<!-- routivus-skill: name=demo description="演示规范" -->\n正文内容', encoding="utf-8"
    )
    (skills / "guide.md").write_text("参考内容", encoding="utf-8")
    return SkillRegistry(project_root=tmp_path, config=config or SkillConfig(), builtin_root=tmp_path / "none")


def test_index_is_metadata_only_and_load_can_include_reference(tmp_path):
    registry = make_registry(tmp_path)
    index = registry.index_text()
    assert "demo" in index and "正文内容" not in index

    document = registry.load(SkillLoadRequest("demo", ("references/guide.md",)))
    assert document.body == "正文内容"
    assert document.references[0].content == "参考内容"
    assert "不是系统指令" in registry.manual_load("demo", ())[1]


def test_cache_is_invalidated_when_body_changes_even_if_size_does_not(tmp_path):
    registry = make_registry(tmp_path)
    first = registry.load("demo")
    path = tmp_path / ".routivus" / "skills" / "demo" / "SKILL.md"
    stat = path.stat()
    path.write_text('<!-- routivus-skill: name=demo -->\n新内容', encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    second = registry.load("demo")
    assert first.body != second.body
    assert second.body == "新内容"


def test_load_rejects_missing_disabled_and_over_limit(tmp_path):
    registry = make_registry(tmp_path, SkillConfig(max_skill_chars=2))
    with pytest.raises(SkillNotFoundError):
        registry.load("missing")
    with pytest.raises(SkillContentError):
        registry.load("demo")

    disabled = make_registry(tmp_path / "disabled", SkillConfig(default_enabled=False))
    with pytest.raises(SkillDisabledError):
        disabled.load("demo")


def test_reference_limits_and_invalid_paths_are_enforced(tmp_path):
    registry = make_registry(tmp_path, SkillConfig(max_reference_chars=2))
    with pytest.raises(SkillContentError):
        registry.load(SkillLoadRequest("demo", ("references/guide.md",)))
    ok, message = registry.manual_load("demo", ("../outside.md",))
    assert not ok
    assert "安全原因" in message
