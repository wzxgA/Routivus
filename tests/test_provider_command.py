"""测试 /provider 命令（execute_provider_command / CommandService 路由）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_config import make_manager
from tests.test_provider_service import raw_manager
from routivus.cli.commands import (
    CommandContext,
    CommandResult,
    CommandService,
    execute_provider_command,
)


def cmd(manager, raw):
    return execute_provider_command(manager, None, raw)


def test_default_usage_when_no_subcommand(tmp_path: Path):
    manager = raw_manager(tmp_path)
    msg, ok = cmd(manager, "/provider")
    assert ok is True
    assert "尚未配置任何 provider" in msg


def test_add_full_args_sets_base(tmp_path: Path):
    manager = raw_manager(tmp_path)
    msg, ok = cmd(manager, "/provider add myproxy https://gateway.my.com/v1 --model deepseek-v4 --set-base")
    assert ok is True
    assert "已添加 provider: myproxy" in msg
    assert "base 已切换: myproxy / deepseek-v4" in msg
    cfg = manager._merged_config()
    assert cfg["active_provider"] == "myproxy"
    assert cfg["active_model"] == "deepseek-v4"


def test_add_rejects_backtick(tmp_path: Path):
    manager = raw_manager(tmp_path)
    msg, ok = cmd(manager, "/provider add bad `https://x.com/v1 --model m")
    assert ok is False
    assert "反引号" in msg


def test_add_rejects_duplicate(tmp_path: Path):
    manager = make_manager(tmp_path)
    msg, ok = cmd(manager, "/provider add openai https://x.com/v1 --model m")
    assert ok is False
    assert "已存在" in msg


def test_show_masks_key(tmp_path: Path):
    manager = make_manager(tmp_path, env={"XG_OPENAI_API_KEY": "sk-abcd12345"})
    msg, ok = cmd(manager, "/provider show openai")
    assert ok is True
    assert "sk-abcd12345" not in msg  # 不暴露明文
    assert "api_key" in msg


def test_set_field(tmp_path: Path):
    manager = make_manager(tmp_path)
    msg, ok = cmd(manager, "/provider set openai default_model gpt-5o")
    assert ok is True
    assert manager.get_config_value("providers.openai.default_model") == "gpt-5o"


def test_switch(tmp_path: Path):
    manager = make_manager(tmp_path, user_cfg={"active_provider": "openai"})
    msg, ok = cmd(manager, "/provider switch deepseek")
    assert ok is True
    assert manager.active().provider_name == "deepseek"


def test_remove_needs_yes_and_refuses_base(tmp_path: Path):
    manager = make_manager(tmp_path, user_cfg={"active_provider": "openai"})  # active base = openai
    msg, ok = cmd(manager, "/provider remove openai --yes")
    assert ok is False
    assert "不能删除当前 base provider" in msg
    assert manager.resolve_provider("openai") is not None  # base 未被删除
    # 非 base provider 需 --yes
    msg, ok = cmd(manager, "/provider remove deepseek")
    assert ok is False
    assert "--yes" in msg
    assert manager.resolve_provider("deepseek") is not None
    msg, ok = cmd(manager, "/provider remove deepseek --yes")
    assert ok is True
    assert "已删除 provider: deepseek" in msg
    assert manager.resolve_provider("deepseek") is None
    # 传错 provider 名
    msg, ok = cmd(manager, "/provider remove nope --yes")
    assert ok is False
    assert "未知 provider" in msg


def test_model_add_and_rm(tmp_path: Path):
    manager = make_manager(tmp_path)
    msg, ok = cmd(manager, "/provider openai model gpt-4o")
    assert ok is True
    assert "已为 openai 添加模型: gpt-4o" in msg
    assert "gpt-4o" in manager.resolve_provider("openai").models  # type: ignore[union-attr]

    # 重复添加被拦截
    msg, ok = cmd(manager, "/provider openai model gpt-4o")
    assert ok is False
    assert "已存在" in msg

    # 删除模型
    msg, ok = cmd(manager, "/provider openai model rm gpt-4o")
    assert ok is True
    assert "已从 openai 移除模型: gpt-4o" in msg
    assert "gpt-4o" not in manager.resolve_provider("openai").models  # type: ignore[union-attr]

    # 删除不存在的模型 → 报错
    msg, ok = cmd(manager, "/provider openai model rm nope")
    assert ok is False
    assert "不在列表" in msg


def test_model_unknown_provider(tmp_path: Path):
    manager = raw_manager(tmp_path)
    msg, ok = cmd(manager, "/provider nope model x")
    assert ok is False
    assert "未知 provider" in msg


def test_model_default_usage_when_incomplete(tmp_path: Path):
    manager = make_manager(tmp_path)
    msg, ok = cmd(manager, "/provider openai model")
    assert ok is False
    assert "model" in msg and "<name>" in msg


def test_key_write_needs_yes_when_exists(tmp_path: Path):
    manager = make_manager(
        tmp_path, user_cfg={"providers": {"deepseek": {"api_key": "old"}}}
    )
    msg, ok = cmd(manager, "/provider key deepseek new")
    assert ok is False
    assert "已配置 key" in msg
    assert "覆盖请加 --yes" in msg
    msg, ok = cmd(manager, "/provider key deepseek new --yes")
    assert ok is True
    assert manager.resolve_provider("deepseek").api_key == "new"  # type: ignore[union-attr]


def test_command_service_routes_provider(tmp_path: Path):
    class FakeSettings:
        provider = ""
        model = ""

    class FakeAgent:
        input_history = None

    manager = raw_manager(tmp_path)
    ctx = CommandContext(agent=FakeAgent(), settings=FakeSettings(), manager=manager)
    result = asyncio.run(CommandService(ctx).execute("/provider"))
    assert isinstance(result, CommandResult)
    assert "尚未配置任何 provider" in result.message


def test_provider_and_tier_commands_render_english(tmp_path: Path):
    manager = raw_manager(tmp_path)
    msg, ok = execute_provider_command(
        manager, None,
        "/provider add myproxy https://gateway.my.com/v1 --model deepseek-v4",
        language="en",
    )
    assert ok is True
    assert "Added provider: myproxy" in msg
    assert "写入" not in msg
    msg, ok = execute_provider_command(manager, None, "/provider show myproxy", language="en")
    assert ok is True
    assert "config layer" in msg
    from routivus.cli.commands import execute_tier_command
    msg, ok = execute_tier_command(manager, None, "/tier set Basic myproxy", language="en")
    assert ok is True
    assert "Tier Basic" in msg
    assert "已" not in msg
