"""测试 :class:`routivus.config.provider_service.ProviderConfigService`。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routivus.config.manager import ConfigManager
from routivus.config.provider_service import (
    ProviderConfigService,
    validate_api_base,
    validate_model,
    validate_name,
)
from tests.test_config import make_manager


def raw_manager(tmp_path: Path, env: dict | None = None, user_cfg: dict | None = None) -> ConfigManager:
    """构造不注入默认 providers 的 manager（用于测试干净的增删状态）。"""
    user_dir = tmp_path / "user_xg"
    project_dir = tmp_path / "proj_xg"
    user_dir.mkdir(exist_ok=True)
    project_dir.mkdir(exist_ok=True)
    if user_cfg is not None:
        (user_dir / "config.json").write_text(json.dumps(user_cfg), encoding="utf-8")
    return ConfigManager(user_dir=user_dir, project_dir=project_dir,
                         env=dict(env or {}), load_env=False)


def test_validate_name():
    assert validate_name("") is not None
    assert validate_name("my-proxy") is None
    assert validate_name("my_proxy") is None
    assert validate_name("my proxy") is not None  # 空格非法
    assert validate_name("foo`bar") is not None  # 反引号非法


def test_validate_api_base():
    assert validate_api_base("") is not None
    assert validate_api_base("  ") is not None
    assert validate_api_base("`https://example.com") is not None  # 反引号
    assert validate_api_base("https:// example.com/v1") is not None  # 空格
    assert validate_api_base("https://api.example.com/v1") is None
    assert validate_api_base("http://api.example.com") is None
    assert validate_api_base("ftp://api.example.com") is not None  # 非法 scheme
    assert validate_api_base("//example.com") is not None  # 缺 scheme


def test_validate_model():
    assert validate_model("") is not None
    assert validate_model("  ") is not None
    assert validate_model("my model") is not None  # 空格非法
    assert validate_model("deepseek-r1") is None
    assert validate_model("asdf`r1") is not None  # 反引号非法


class TestService:
    def test_list_empty_when_none_configured(self, tmp_path: Path):
        manager = raw_manager(tmp_path)
        svc = ProviderConfigService(manager)
        assert len(svc.list()) == 0

    def test_add_new_provider_passes_validation(self, tmp_path: Path):
        manager = make_manager(tmp_path, {"ROUTIVUS_MYPROXY_API_KEY": "sk-test"})
        svc = ProviderConfigService(manager)
        res = svc.add("myproxy", "https://api.myproxy.com/v1", "mymodel", "MyProxy")
        assert res.ok
        assert "已添加" in res.message
        providers = json.loads((manager.user_config_path).read_text())["providers"]
        assert providers["myproxy"]["api_base"] == "https://api.myproxy.com/v1"
        assert providers["myproxy"]["default_model"] == "mymodel"
        assert providers["myproxy"]["display_name"] == "MyProxy"
        assert len(svc.list()) == 4 + 1  # conftest.DEFAULT_PROVIDERS + myproxy

    def test_add_rejects_duplicate(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.add("openai", "https://x.com/v1", "gpt-x", "X")
        assert not res.ok
        assert "已存在" in res.message

    def test_add_rejects_bad_api_base(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.add("bad", "`https://x.com/v1", "m")
        assert not res.ok
        assert "反引号" in res.message

    def test_add_rejects_empty_default_model(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.add("bad", "https://x.com/v1", "")
        assert not res.ok
        assert "default_model 必填" in res.message

    def test_update_partial_fields(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.update("openai", {"default_model": "gpt-5o"})
        assert res.ok
        cfg = json.loads((manager.user_config_path).read_text())["providers"]["openai"]
        assert cfg["default_model"] == "gpt-5o"
        assert cfg["api_base"] == "https://api.openai.com/v1"  # 保持不变

    def test_update_unknown_field_rejected(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.update("openai", {"unknown": "value"})
        assert not res.ok
        assert "不支持字段" in res.message

    def test_add_model_and_remove_model(self, tmp_path: Path):
        manager = make_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.add_model("openai", "gpt-4o")
        assert res.ok
        assert "已为 openai 添加模型: gpt-4o" in res.message
        assert "gpt-4o" in svc.get("openai")["models"]
        openai_row = next(r for r in svc.list() if r["name"] == "openai")
        assert list(openai_row["models"]) == ["gpt-4o"]
        # 重复添加拦截
        res = svc.add_model("openai", "gpt-4o")
        assert not res.ok
        assert "已存在" in res.message
        # 移除
        res = svc.remove_model("openai", "gpt-4o")
        assert res.ok
        assert "gpt-4o" not in svc.get("openai")["models"]

    def test_add_model_rejects_bad_model_or_unknown_provider(self, tmp_path: Path):
        manager = raw_manager(tmp_path)
        svc = ProviderConfigService(manager)
        res = svc.add_model("nope", "x")
        assert not res.ok
        assert "未知 provider" in res.message
        manager.upsert_provider("p", {"api_base": "https://x.com/v1", "default_model": "m"})
        res = svc.add_model("p", "bad model")
        assert not res.ok
        assert "模型名" in res.message or "非法字符" in res.message

    def test_remove_requires_yes(self, tmp_path: Path):
        manager = make_manager(tmp_path)  # active base 为 openai
        svc = ProviderConfigService(manager)
        res = svc.remove("deepseek", yes=False)
        assert not res.ok
        assert "确认请加 --yes" in res.message
        res = svc.remove("deepseek", yes=True)
        assert res.ok
        assert svc.get("deepseek") is None

    def test_remove_refuses_active_base(self, tmp_path: Path):
        manager = make_manager(tmp_path, user_cfg={"active_provider": "openai"})  # active base 为 openai
        svc = ProviderConfigService(manager)
        res = svc.remove("openai", yes=True)
        assert not res.ok
        assert "不能删除当前 base provider" in res.message
        assert svc.get("openai") is not None

    def test_switch_base_changes_active(self, tmp_path: Path):
        manager = make_manager(tmp_path, user_cfg={"active_provider": "openai"})
        svc = ProviderConfigService(manager)
        res = svc.switch("deepseek", "deepseek-reasoner")
        assert res.ok
        # set_active 持久化 active_provider / active_model
        active = manager.active()
        assert active.provider_name == "deepseek"
        assert active.model == "deepseek-reasoner"

    def test_set_api_key_writes_to_config(self, tmp_path: Path):
        manager = raw_manager(tmp_path)
        manager.upsert_provider("openai", {"api_base": "https://x.com/v1", "default_model": "m"})
        svc = ProviderConfigService(manager)
        res = svc.set_api_key("openai", "new-key", overwrite=False)
        # 已有值、无 --yes 不覆盖（初次就是无，所以覆盖默认 false 也能写）
        assert res.ok
        # 写入 config.json 的 providers.openai.api_key
        row = svc.get("openai")
        assert row is not None
        assert row["has_key"]
        assert row["api_key"] == "new-key"
        assert row["api_key_masked"] == "new-****"
        # 从 manager.resolve 能读出 key
        p = manager.resolve_provider("openai")
        assert p is not None
        assert p.api_key == "new-key"

    def test_set_api_key_overwrite_needs_confirmation(self, tmp_path: Path):
        manager = raw_manager(tmp_path)
        manager.upsert_provider("openai", {"api_base": "https://x.com/v1", "default_model": "m"})
        svc = ProviderConfigService(manager)
        svc.set_api_key("openai", "old-key")
        res = svc.set_api_key("openai", "new-key", overwrite=False)
        assert not res.ok
        assert "已配置 key" in res.message
        assert "覆盖请加 --yes" in res.message
        # 未确认前不写
        assert svc.get("openai")["api_key"] == "old-key"
        res = svc.set_api_key("openai", "new-key", yes=True)
        assert res.ok
        assert svc.get("openai")["api_key"] == "new-key"

    def test_referenced_by_returns_smartrouter_tiers(self, tmp_path: Path):
        manager = make_manager(tmp_path, user_cfg={
            "smart_router": {
                "enabled": True,
                "tiers": {
                    "Basic": {"provider": "openai"},
                    "Enhanced": {"provider": "deepseek"},
                }
            }
        })
        svc = ProviderConfigService(manager)
        assert set(svc.referenced_by("openai")) == {"Basic"}
        assert set(svc.referenced_by("deepseek")) == {"Enhanced"}
        assert len(svc.referenced_by("kimi")) == 0
