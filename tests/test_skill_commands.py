from __future__ import annotations

import json

import pytest

from routivus.cli.commands import execute_skill_command
from routivus.skill.models import SkillConfig
from routivus.skill.registry import SkillRegistry


class Agent:
    pass


def make_agent(tmp_path):
    path = tmp_path / ".routivus" / "skills" / "demo"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("<!-- routivus-skill: name=demo -->\n规范", encoding="utf-8")
    user = tmp_path / "user"
    manager_module = __import__("routivus.config.skills", fromlist=["SkillConfigManager"])
    manager = manager_module.SkillConfigManager(user_dir=user, project_root=tmp_path, env={})
    agent = Agent()
    agent.skill_registry = SkillRegistry(
        project_root=tmp_path, config=SkillConfig(), config_manager=manager, builtin_root=tmp_path / "none"
    )
    return agent, manager


@pytest.mark.asyncio
async def test_skill_commands_list_load_enable_disable(tmp_path):
    agent, manager = make_agent(tmp_path)
    message, ok = await execute_skill_command(agent, "/skill list")
    assert ok and "demo" in message
    message, ok = await execute_skill_command(agent, "/skill load demo")
    assert ok and "规范" in message
    message, ok = await execute_skill_command(agent, "/skill disable demo")
    assert ok and "已禁用" in message
    saved = json.loads((tmp_path / ".routivus" / "skills.json").read_text(encoding="utf-8"))
    assert saved["enabled"]["demo"] is False
    message, ok = await execute_skill_command(agent, "/skill enable demo")
    assert ok and "已启用" in message


@pytest.mark.asyncio
async def test_skill_command_usage_and_disabled_state(tmp_path):
    agent, _ = make_agent(tmp_path)
    message, ok = await execute_skill_command(agent, "/skill load")
    assert not ok and "用法" in message
    agent.skill_registry.config = SkillConfig(enabled=False)
    message, ok = await execute_skill_command(agent, "/skill list")
    assert not ok and "未启用" in message
