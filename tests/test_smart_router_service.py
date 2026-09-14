"""测试 :class:`routivus.config.smart_router_service.SmartRouterConfigService`。"""

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


def test_list_tiers_reports_resolved_target(tmp_path: Path):
    """方案 08 §4.3：列表同时给出"显式配置（provider/model/configured）"与
    "实际会用（resolved_*）"。

    `configured` 表示**是否显式配置**；`resolved_*` 是经 `resolve_tier` 校验后的
    真实目标——未配置、或 provider 缺 API Key 时都会整档回落 active，界面据此
    显示"回落 → deepseek · deepseek-chat"，而不是只有一句"回落 active"。
    """
    manager = make_manager(
        tmp_path,
        env={},
        user_cfg={
            "active_provider": "deepseek",
            "active_model": "deepseek-chat",
            "providers": {"deepseek": {"api_key": "sk-test"}, "glm": {"api_key": "gk"}},
            "smart_router": {
                "enabled": True,
                "tiers": {"Superior": {"provider": "glm", "model": "glm-4-plus"}},
            },
        },
    )
    rows = {row["name"]: row for row in SmartRouterConfigService(manager).list_tiers()}

    assert rows["Superior"]["configured"] is True
    assert rows["Superior"]["resolved_provider"] == "glm"
    assert rows["Superior"]["resolved_model"] == "glm-4-plus"

    assert rows["Basic"]["configured"] is False
    assert rows["Basic"]["provider"] == ""                    # 显式配置为空
    assert rows["Basic"]["resolved_provider"] == "deepseek"   # 实际回落目标
    assert rows["Basic"]["resolved_model"] == "deepseek-chat"


def test_list_tiers_resolved_falls_back_without_key(tmp_path: Path):
    """配了档位但该 provider 缺 API Key：`configured` 仍为 True，但实际会回落 active。"""
    manager = make_manager(
        tmp_path,
        env={},
        user_cfg={
            "active_provider": "deepseek",
            "active_model": "deepseek-chat",
            "providers": {"deepseek": {"api_key": "sk-test"}},   # glm 无 Key
            "smart_router": {
                "enabled": True,
                "tiers": {"Superior": {"provider": "glm", "model": "glm-4-plus"}},
            },
        },
    )
    row = {item["name"]: item for item in SmartRouterConfigService(manager).list_tiers()}["Superior"]

    assert row["configured"] is True                          # 用户确实配了
    assert row["provider"] == "glm" and row["model"] == "glm-4-plus"
    assert row["resolved_provider"] == "deepseek"             # 但实际回落
    assert row["resolved_model"] == "deepseek-chat"