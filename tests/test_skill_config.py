from __future__ import annotations

import json

from routivus.config.skills import SkillConfigManager


def test_project_skill_config_overrides_user_and_merges_enabled(tmp_path):
    user = tmp_path / "user"
    project = tmp_path / "project"
    user.mkdir()
    (project / ".routivus").mkdir(parents=True)
    (user / "skills.json").write_text(json.dumps({
        "enabled": {"user-skill": False, "shared": False},
        "max_index_items": 9,
    }), encoding="utf-8")
    (project / ".routivus" / "skills.json").write_text(json.dumps({
        "enabled": {"project-skill": True, "shared": True},
        "max_index_chars": 1234,
    }), encoding="utf-8")

    manager = SkillConfigManager(user_dir=user, project_root=project, env={})
    config = manager.load()

    assert config.default_enabled is True
    assert config.max_index_items == 9
    assert config.max_index_chars == 1234
    assert manager.enabled_overrides() == {
        "user-skill": False, "shared": True, "project-skill": True,
    }


def test_environment_overrides_skill_switch_and_limits(tmp_path):
    config = SkillConfigManager(
        user_dir=tmp_path / "user", project_root=tmp_path,
        env={
            "ROUTIVUS_SKILLS_ENABLED": "off",
            "ROUTIVUS_SKILLS_MAX_INDEX_ITEMS": "3",
            "ROUTIVUS_SKILLS_MAX_CHARS": "777",
        },
    ).load()
    assert config.enabled is False
    assert config.max_index_items == 3
    assert config.max_skill_chars == 777


def test_set_enabled_writes_project_or_user_layer(tmp_path):
    user = tmp_path / "user"
    project = tmp_path / "project"
    manager = SkillConfigManager(user_dir=user, project_root=project, env={})

    manager.set_enabled("project-skill", False, project=True)
    manager.set_enabled("user-skill", False, project=False)

    assert json.loads((project / ".routivus" / "skills.json").read_text(encoding="utf-8"))["enabled"] == {
        "project-skill": False,
    }
    assert json.loads((user / "skills.json").read_text(encoding="utf-8"))["enabled"] == {
        "user-skill": False,
    }
