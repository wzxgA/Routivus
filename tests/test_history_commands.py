from __future__ import annotations

import pytest

from routivus.cli.commands import execute_history_command
from routivus.input_history import InputHistory


class Agent:
    pass


@pytest.mark.asyncio
async def test_history_status_and_clear_do_not_expose_entries(tmp_path):
    agent = Agent()
    agent.input_history = InputHistory(project_root=tmp_path, user_dir=tmp_path / "user")
    agent.input_history.add("这是不应被命令全文展示的输入")

    message, ok = await execute_history_command(agent, "/history status")
    assert ok
    assert "1 条" in message
    assert "这是不应被命令全文展示的输入" not in message

    message, ok = await execute_history_command(agent, "/history clear")
    assert ok and "1 条" in message
    assert agent.input_history.entries() == ()

    message, ok = await execute_history_command(agent, "/history unknown")
    assert not ok and "用法" in message
