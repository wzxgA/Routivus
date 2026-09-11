"""SKILL.md 受控写入（配置页「技能」页新建/编辑）的单测。

只验写入侧的边界：落点、名称契约、大小上限、元信息渲染。读取侧的解析与
路径白名单见 tests/test_skill_registry.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from routivus.skill.errors import SkillContentError, SkillParseError, SkillSecurityError
from routivus.skill.parser import read_metadata
from routivus.skill.writer import SkillWriteRequest, render_document, write_skill_document


def _write(tmp_path: Path, **overrides: object) -> tuple[Path, bool]:
    payload = {
        "name": "demo",
        "body": "# 规范\n\n- 先写测试",
        "description": "演示规范",
        "layer": "user",
    }
    payload.update(overrides)
    return write_skill_document(
        SkillWriteRequest(**payload),  # type: ignore[arg-type]
        user_dir=tmp_path / "user",
        project_root=tmp_path / "proj",
        max_chars=32_000,
    )


def test_write_user_skill_creates_readable_document(tmp_path: Path) -> None:
    path, created = _write(tmp_path)

    assert created is True
    assert path == (tmp_path / "user" / "skills" / "demo" / "SKILL.md").resolve()
    metadata = read_metadata(path.parent, name="demo", source="user")
    assert metadata["description"] == "演示规范"
    assert "先写测试" in path.read_text(encoding="utf-8")


def test_rewrite_reports_not_created(tmp_path: Path) -> None:
    _write(tmp_path)
    _, created = _write(tmp_path, body="改过的正文")
    assert created is False


def test_write_project_layer(tmp_path: Path) -> None:
    path, _ = _write(tmp_path, layer="project")
    assert path == (tmp_path / "proj" / ".routivus" / "skills" / "demo" / "SKILL.md").resolve()


def test_project_layer_requires_context(tmp_path: Path) -> None:
    with pytest.raises(SkillSecurityError):
        write_skill_document(
            SkillWriteRequest(name="demo", body="x", layer="project"),
            user_dir=tmp_path / "user",
            project_root=None,
            max_chars=32_000,
        )


def test_unknown_layer_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SkillSecurityError):
        _write(tmp_path, layer="builtin")


@pytest.mark.parametrize("name", ["../escape", "Upper", "with space", "带中文", "", "a/b", ".hidden"])
def test_invalid_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(SkillParseError):
        _write(tmp_path, name=name)


def test_oversized_body_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SkillContentError):
        _write(tmp_path, body="x" * 40_000)


def test_render_document_escapes_description_quotes() -> None:
    text = render_document("demo", '含"引号"的说明', "正文")
    assert text.startswith("<!-- routivus-skill: name=demo")
    assert "'引号'" in text
    assert text.endswith("正文\n")
