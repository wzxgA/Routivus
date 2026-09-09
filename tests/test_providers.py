"""Provider 注册表单元测试。

provider 无内置预设，全部由用户自定义。
"""

from __future__ import annotations

from routivus.config.providers import Provider, ProviderRegistry


class TestRegistry:
    def test_empty_by_default(self):
        """注册表初始为空——不再内置 openai/deepseek/glm/kimi。"""
        assert ProviderRegistry().all() == []

    def test_get_unknown_returns_none(self):
        assert ProviderRegistry().get("nope") is None

    def test_register_custom(self):
        registry = ProviderRegistry()
        registry.register(
            Provider("custom", "Custom", "https://x/v1", "ROUTIVUS_CUSTOM_API_KEY", "m", 64000)
        )
        assert "custom" in registry.names()
        assert registry.get("custom").api_base == "https://x/v1"

    def test_create_from_seed_list(self):
        p = Provider("local", "Local", "http://localhost:8080/v1", "ROUTIVUS_LOCAL_API_KEY", "m", 8192)
        registry = ProviderRegistry([p])
        assert registry.get("local").display_name == "Local"

    def test_register_overwrites(self):
        registry = ProviderRegistry()
        registry.register(Provider("a", "A", "u", "k", "m", 1))
        registry.register(Provider("a", "A2", "u", "k", "m", 1))
        assert registry.get("a").display_name == "A2"