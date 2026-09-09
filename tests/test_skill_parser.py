from __future__ import annotations

from pathlib import Path

import pytest

from routivus.skill.errors import SkillParseError
from routivus.skill.parser import parse_metadata, read_metadata, validate_name


def test_metadata_preserves_utf8_and_decodes_only_contract_escapes(tmp_path: Path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        '# Demo\n\n<!-- xg-skill: name=demo version="1\\.0" description="中文 \\"规范\\"" -->\n\n正文',
        encoding="utf-8",
    )

    info = read_metadata(root, name="demo", source="project")

    assert info["description"] == '中文 "规范"'
    assert info["version"] == "1\\.0"


def test_description_falls_back_to_first_body_paragraph(tmp_path: Path):
    data = parse_metadata("demo", "# 标题\n\n**第一段** 说明\n\n第二段", source="builtin", root=tmp_path)
    assert data["description"] == "第一段 说明"


@pytest.mark.parametrize("name", ["", "Demo", "a/b", "a..b", "-bad", "a" * 65])
def test_skill_name_is_strict(name: str):
    with pytest.raises(SkillParseError):
        validate_name(name)


def test_metadata_name_must_match_directory(tmp_path: Path):
    with pytest.raises(SkillParseError, match="目录名"):
        parse_metadata(
            "demo", '<!-- xg-skill: name=other description="x" -->\n正文',
            source="project", root=tmp_path,
        )


def test_malformed_metadata_is_rejected(tmp_path: Path):
    with pytest.raises(SkillParseError):
        parse_metadata("demo", "<!-- xg-skill: name=demo broken -->\n正文", source="project", root=tmp_path)


def test_invalid_utf8_is_rejected(tmp_path: Path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"\xff\xfe")
    with pytest.raises(SkillParseError):
        read_metadata(root, name="demo", source="project")
