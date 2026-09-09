from __future__ import annotations

import pytest

from routivus.skill.errors import SkillSecurityError
from routivus.skill.policy import validate_reference_path


@pytest.mark.parametrize("raw", ["../x.md", "/tmp/x.md", "references/../x.md", "references\\..\\x.md", ""])
def test_reference_path_cannot_escape_references_directory(tmp_path, raw):
    root = tmp_path / "skill"
    (root / "references").mkdir(parents=True)
    with pytest.raises(SkillSecurityError):
        validate_reference_path(root, raw)


def test_reference_type_is_allowlisted(tmp_path):
    root = tmp_path / "skill"
    (root / "references").mkdir(parents=True)
    (root / "references" / "payload.exe").write_bytes(b"not text")
    with pytest.raises(SkillSecurityError, match="类型"):
        validate_reference_path(root, "references/payload.exe")


def test_symlink_escape_is_rejected_when_platform_allows_symlinks(tmp_path):
    root = tmp_path / "skill"
    outside = tmp_path / "outside.md"
    (root / "references").mkdir(parents=True)
    outside.write_text("secret", encoding="utf-8")
    try:
        (root / "references" / "outside.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(SkillSecurityError):
        validate_reference_path(root, "references/outside.md")
