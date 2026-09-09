from __future__ import annotations

from routivus.skill.loader import SkillLoader
from routivus.skill.models import SkillConfig, SkillRoot


def write_skill(root, name, body, metadata=None):
    path = root / name
    path.mkdir(parents=True)
    comment = metadata or f'name={name} description="{name} description"'
    (path / "SKILL.md").write_text(f"<!-- xg-skill: {comment} -->\n{body}", encoding="utf-8")
    return path


def test_three_layers_use_project_precedence_and_keep_source(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    write_skill(builtin, "shared", "builtin")
    write_skill(user, "shared", "user")
    write_skill(user, "user-only", "user")
    write_skill(project, "shared", "project")
    write_skill(project, "broken", "<!-- xg-skill: name=wrong -->\nbody")

    infos = SkillLoader(
        (SkillRoot("builtin", builtin), SkillRoot("user", user), SkillRoot("project", project)),
        SkillConfig(),
    ).discover()
    by_name = {info.name: info for info in infos}

    assert by_name["shared"].source == "project"
    assert by_name["shared"].root == project / "shared"
    assert by_name["broken"].valid is False
    assert by_name["user-only"].source == "user"


def test_disabled_override_is_applied_during_discovery(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", "body")
    infos = SkillLoader((SkillRoot("project", root),), SkillConfig()).discover({"demo": False})
    assert infos[0].enabled is False
