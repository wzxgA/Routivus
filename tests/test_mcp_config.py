"""MCP configuration merge, validation and secret expansion tests."""

from __future__ import annotations

import json

from routivus.config.mcp import McpConfigManager


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_user_project_merge_and_environment_expansion(tmp_path):
    user = tmp_path / "user"
    _write(user / "mcp.json", {
        "servers": {
            "github": {
                "transport": "streamable_http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                "request_timeout": 30,
            }
        }
    })
    _write(tmp_path / ".routivus" / "mcp.json", {
        "servers": {"github": {"request_timeout": 45, "hitl": "always"}}
    })
    manager = McpConfigManager(
        user_dir=user, project_root=tmp_path, env={"MCP_TOKEN": "secret-value"}
    )

    loaded = manager.load()

    assert loaded.errors == ()
    server = loaded.servers["github"]
    assert server.request_timeout == 45
    assert server.hitl == "always"
    assert server.headers["Authorization"] == "Bearer secret-value"


def test_missing_environment_variable_disables_only_that_server(tmp_path):
    _write(tmp_path / ".routivus" / "mcp.json", {
        "servers": {
            "bad": {"transport": "stdio", "command": "x", "env": {"TOKEN": "${MISSING}"}},
            "good": {"transport": "stdio", "command": "x"},
        }
    })
    loaded = McpConfigManager(project_root=tmp_path, env={}).load()
    assert set(loaded.servers) == {"good"}
    assert any("MISSING" in error for error in loaded.errors)


def test_rejects_unsafe_url_and_outside_cwd(tmp_path):
    _write(tmp_path / ".routivus" / "mcp.json", {
        "servers": {
            "file_url": {"transport": "streamable_http", "url": "file:///tmp/mcp"},
            "outside": {"transport": "stdio", "command": "x", "cwd": "../outside"},
        }
    })
    loaded = McpConfigManager(project_root=tmp_path, env={}).load()
    assert not loaded.servers
    assert len(loaded.errors) == 2


def test_enable_override_has_priority_without_copying_server_secrets(tmp_path):
    user = tmp_path / "user"
    _write(tmp_path / ".routivus" / "mcp.json", {
        "servers": {"project": {"enabled": True, "transport": "stdio", "command": "x"}}
    })
    manager = McpConfigManager(user_dir=user, project_root=tmp_path, env={})
    manager.set_enabled("project", False)
    loaded = manager.load()
    assert loaded.servers["project"].enabled is False
    persisted = json.loads((user / "mcp.json").read_text(encoding="utf-8"))
    assert persisted == {"enabled_overrides": {"project": False}}

