from __future__ import annotations

from routivus.config.web import WebConfigManager
from routivus.tool.builtin import build_registry


def test_web_tools_are_registered_only_when_enabled(tmp_path):
    disabled = WebConfigManager(project_root=tmp_path, env={"ROUTIVUS_WEB_ENABLED": "off"}).load()
    assert "web_search" not in build_registry(base_dir=tmp_path, web_config=disabled).names()
    enabled = WebConfigManager(project_root=tmp_path, env={"ROUTIVUS_WEB_ENABLED": "on"}).load()
    registry = build_registry(base_dir=tmp_path, web_config=enabled)
    assert "web_search" in registry.names()
    assert "web_fetch" in registry.names()
    assert registry.get("web_search").source == "builtin-web"
