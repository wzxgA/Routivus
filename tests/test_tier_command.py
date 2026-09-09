"""测试 /tier 命令（execute_tier_command / CommandService 路由）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_smart_router_service import svc
from routivus.cli.commands import (
    CommandContext,
    CommandResult,
    CommandService,
    execute_tier_command,
)


def cmd(manager, raw):
    return execute_tier_command(manager, None, raw)


def test_default_list_when_no_subcommand(tmp_path: Path):
    _, manager = svc(tmp_path)
    msg, ok = cmd(manager, "/tier")
    assert ok is True
    assert "Basic" in msg and "Ultimate" in msg


def test_list_shows_configured(tmp_path: Path):
    _, manager = svc(tmp_path)
    from routivus.config.smart_router_service import SmartRouterConfigService

    SmartRouterConfigService(manager).set_tier("Basic", "deepseek", "deepseek-chat")
    msg, ok = cmd(manager, "/tier list")
    assert ok is True
    assert "deepseek" in msg


def test_show_tier(tmp_path: Path):
    _, manager = svc(tmp_path)
    msg, ok = cmd(manager, "/tier show Basic")
    assert ok is True
    assert "Basic" in msg


def test_show_unknown_tier(tmp_path: Path):
    _, manager = svc(tmp_path)
    msg, ok = cmd(manager, "/tier show Review")
    assert ok is False
    assert "未知档位" in msg


def test_set_tier_persists(tmp_path: Path):
    _, manager = svc(tmp_path)
    msg, ok = cmd(manager, "/tier set Basic deepseek deepseek-chat")
    assert ok is True
    assert "Basic" in msg
    assert manager.smart_router_config()["tiers"]["Basic"] == {
        "provider": "deepseek", "model": "deepseek-chat",
    }


def test_set_tier_unknown_provider(tmp_path: Path):
    _, manager = svc(tmp_path)
    msg, ok = cmd(manager, "/tier set Basic nope")
    assert ok is False
    assert "未知 provider" in msg


def test_clear_tier(tmp_path: Path):
    _, manager = svc(tmp_path)
    cmd(manager, "/tier set Basic deepseek deepseek-chat")
    msg, ok = cmd(manager, "/tier clear Basic")
    assert ok is True
    assert "已清空" in msg
    assert manager.smart_router_config()["tiers"].get("Basic") in (None, {})


def test_command_service_routes_tier(tmp_path: Path):
    class FakeAgent:
        input_history = None

    class FakeSettings:
        provider = ""
        model = ""

    _, manager = svc(tmp_path)
    ctx = CommandContext(agent=FakeAgent(), settings=FakeSettings(), manager=manager)
    result = asyncio.run(CommandService(ctx).execute("/tier"))
    assert isinstance(result, CommandResult)
    assert "Basic" in result.message