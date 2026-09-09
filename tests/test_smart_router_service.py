"""测试 :class:`xg.config.smart_router_service.SmartRouterConfigService`。"""

from __future__ import annotations

from pathlib import Path

from routivus.config.smart_router_service import SmartRouterConfigService
from tests.test_config import make_manager


def svc(tmp_path: Path, **kw) -> tuple[SmartRouterConfigService, object]:
    manager = make_manager(tmp_path, env={}, **kw)
    return SmartRouterConfigService(manager), manager


def test_default_empty(tmp_path: Path):
    service, _ = svc(tmp_path)
    assert service.get() == {"enabled": False, "tiers": {}}


def test_list_tiers_fixed_order_and_unconfigured(tmp_path: Path):
    service, _ = svc(tmp_path)
    names = [r["name"] for r in service.list_tiers()]
    assert names == ["Basic", "Enhanced", "Superior", "Ultimate"]
    assert all(not r["configured"] for r in service.list_tiers())


def test_set_tier_uses_default_model_when_omitted(tmp_path: Path):
    service, manager = svc(tmp_path)
    result = service.set_tier("Basic", "deepseek")
    assert result.ok is True
    row = service.get()["tiers"]["Basic"]
    assert row["provider"] == "deepseek"
    assert row["model"] == manager.resolve_provider("deepseek").default_model  # type: ignore[union-attr]


def test_set_tier_with_explicit_model(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.set_tier("Ultimate", "deepseek", "deepseek-reasoner")
    assert result.ok is True
    assert service.get()["tiers"]["Ultimate"] == {
        "provider": "deepseek", "model": "deepseek-reasoner",
    }


def test_set_tier_rejects_unknown_provider(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.set_tier("Basic", "nope")
    assert result.ok is False
    assert "未知 provider" in result.message


def test_set_tier_rejects_unknown_tier(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.set_tier("Review", "deepseek")
    assert result.ok is False
    assert "未知档位" in result.message


def test_set_tier_rejects_invalid_model(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.set_tier("Basic", "deepseek", "bad`model")
    assert result.ok is False


def test_clear_tier(tmp_path: Path):
    service, _ = svc(tmp_path)
    assert service.set_tier("Basic", "deepseek").ok is True
    result = service.clear_tier("Basic")
    assert result.ok is True
    assert "已清空" in result.message
    assert service.get()["tiers"].get("Basic") in (None, {})


def test_clear_unconfigured_tier(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.clear_tier("Basic")
    assert result.ok is True
    assert "未配置" in result.message


def test_clear_unknown_tier(tmp_path: Path):
    service, _ = svc(tmp_path)
    result = service.clear_tier("Review")
    assert result.ok is False


def test_get_tier(tmp_path: Path):
    service, _ = svc(tmp_path)
    service.set_tier("Enhanced", "deepseek", "deepseek-chat")
    result, row = service.get_tier("Enhanced")
    assert result.ok is True
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-chat"


def test_set_enabled(tmp_path: Path):
    service, manager = svc(tmp_path)
    assert service.set_enabled(True).ok is True
    assert manager.smart_router_config()["enabled"] is True