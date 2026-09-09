from __future__ import annotations

import json

from routivus.config.web import WebConfigManager


def test_web_config_project_overrides_user_and_expands_env(tmp_path):
    user = tmp_path / "user"
    project = tmp_path / "project"
    (user).mkdir()
    (project / ".routivus").mkdir(parents=True)
    (user / "web.json").write_text(json.dumps({
        "search": {"provider": "serpapi", "max_results": 8},
        "providers": {"serpapi": {"api_key": "${SERP_KEY}"}},
    }), encoding="utf-8")
    (project / ".routivus" / "web.json").write_text(json.dumps({
        "search": {"provider": "searxng"},
        "providers": {"searxng": {"url": "${SEARX_URL}"}},
    }), encoding="utf-8")
    cfg = WebConfigManager(user_dir=user, project_root=project,
                           env={"SERP_KEY": "secret", "SEARX_URL": "http://search.test"}).load()
    assert cfg.search.provider == "searxng"
    assert cfg.search.max_results == 8
    assert cfg.search.api_base == "http://search.test"
    assert cfg.search.api_key is None


def test_web_config_env_can_disable_without_affecting_file(tmp_path):
    cfg_file = tmp_path / ".routivus"
    cfg_file.mkdir()
    (cfg_file / "web.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    cfg = WebConfigManager(project_root=tmp_path, env={"ROUTIVUS_WEB_ENABLED": "off"}).load()
    assert cfg.enabled is False
