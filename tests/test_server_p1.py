"""P1：Web Console 配置接口、REST 令牌鉴权与桌面端运行时白名单授权。

这组能力是桌面化的前提：没有配置接口，用户无法在界面上填写 provider
（本仓库既无 REPL 也无 CLI 入口），桌面版首次打开就是不可用的。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routivus.config.manager import ConfigManager
from routivus.server.app import create_app
from routivus.server.config import ServerConfig
from routivus.server.logging_setup import RedactTokenFilter
from routivus.server.projects import ProjectRegistry

TOKEN = "secret-token"


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
        **overrides,  # type: ignore[arg-type]
    )
    return TestClient(create_app(registry=registry, config=config))


def _provider_payload(name: str = "myproxy", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "api_base": "https://gateway.example.com/v1",
        "default_model": "deepseek-v4",
    }
    payload.update(overrides)
    return payload


# ---------- 配置快照 ----------


def test_config_snapshot_starts_empty(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = client.get("/api/config").json()

    assert body["providers"] == []
    assert body["active_provider"] == ""
    assert body["active_model"] == ""
    assert body["smart_router_enabled"] is False
    assert [tier["name"] for tier in body["tiers"]] == ["Basic", "Enhanced", "Superior", "Ultimate"]
    assert all(tier["configured"] is False for tier in body["tiers"])
    # 未配置 provider 是正常初始状态，不该报错
    assert body["user_dir"].endswith("userdata")


def test_desktop_info_reports_user_dir_and_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTIVUS_DESKTOP", "1")
    client = _client(tmp_path)
    info = client.get("/api/desktop/info").json()

    assert info["desktop"] is True
    assert info["user_dir"].endswith("userdata")
    assert len(info["allowed_roots"]) == 1
    assert info["allowed_roots"][0].endswith("workspace")
    # 旧目录提示是环境相关的，只要求字段存在
    assert "legacy_user_dir" in info


# ---------- provider CRUD ----------


def test_provider_crud_never_returns_raw_key(tmp_path: Path) -> None:
    client = _client(tmp_path)
    secret = "sk-supersecret-1234"

    created = client.post(
        "/api/config/providers",
        json=_provider_payload(api_key=secret, set_base=True),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "myproxy"
    assert body["has_key"] is True
    assert body["is_base"] is True
    assert body["api_key_masked"] == "sk-s****"
    assert secret not in created.text, "原始 Key 绝不能被接口回显"

    snapshot = client.get("/api/config").json()
    assert snapshot["active_provider"] == "myproxy"
    assert snapshot["active_model"] == "deepseek-v4"
    assert secret not in str(snapshot)

    # 更新字段
    updated = client.patch(
        "/api/config/providers/myproxy", json={"default_model": "deepseek-v4-pro"}
    )
    assert updated.status_code == 200
    assert updated.json()["default_model"] == "deepseek-v4-pro"

    # 模型增删（模型名走 query，避开带斜杠的模型名被拆成路径段）
    added = client.post("/api/config/providers/myproxy/models", json={"model": "meta/llama-3"})
    assert "meta/llama-3" in added.json()["models"]
    removed = client.delete("/api/config/providers/myproxy/models", params={"model": "meta/llama-3"})
    assert "meta/llama-3" not in removed.json()["models"]

    # 覆盖 Key
    rekeyed = client.post("/api/config/providers/myproxy/key", json={"api_key": "sk-new-key-9999"})
    assert rekeyed.status_code == 200
    assert rekeyed.json()["api_key_masked"] == "sk-n****"

    # base provider 不允许直接删除（沿用 provider_service 的既有约束）
    assert client.delete("/api/config/providers/myproxy").status_code == 422


def test_provider_validation_is_reported_as_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bad_base = client.post(
        "/api/config/providers", json=_provider_payload("p1", api_base="ftp://nope")
    )
    assert bad_base.status_code == 422
    assert bad_base.json()["error"]["code"] == "config_rejected"

    bad_name = client.post("/api/config/providers", json=_provider_payload("bad name!"))
    assert bad_name.status_code == 422

    # 重复添加同名 provider
    assert client.post("/api/config/providers", json=_provider_payload("dup")).status_code == 201
    assert client.post("/api/config/providers", json=_provider_payload("dup")).status_code == 422

    # 未知 provider 的操作
    assert client.post("/api/config/providers/ghost/models", json={"model": "m"}).status_code == 422
    assert client.patch("/api/config/providers/ghost", json={"default_model": "m"}).status_code == 404


def test_switch_active_provider_and_model(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/config/providers", json=_provider_payload("p1", default_model="m1"))
    client.post("/api/config/providers", json=_provider_payload("p2", default_model="m2"))

    switched = client.post("/api/config/active", json={"provider": "p2", "model": "m2-turbo"})
    assert switched.status_code == 200
    assert switched.json()["active_provider"] == "p2"
    assert switched.json()["active_model"] == "m2-turbo"

    # 缺省 model 时回落 provider 的 default_model
    fallback = client.post("/api/config/active", json={"provider": "p1"})
    assert fallback.json()["active_model"] == "m1"

    assert client.post("/api/config/active", json={"provider": "ghost"}).status_code == 422


# ---------- SmartRouter 档位 ----------


def test_tier_roundtrip_and_validation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/config/providers", json=_provider_payload("p1", set_base=True))

    set_tier = client.put("/api/config/tiers/Basic", json={"provider": "p1", "model": "m-basic"})
    assert set_tier.status_code == 200
    tiers = {tier["name"]: tier for tier in set_tier.json()["tiers"]}
    assert tiers["Basic"]["configured"] is True
    assert tiers["Basic"]["model"] == "m-basic"

    cleared = client.delete("/api/config/tiers/Basic")
    assert cleared.status_code == 200
    assert {tier["name"]: tier for tier in cleared.json()["tiers"]}["Basic"]["configured"] is False

    assert client.put("/api/config/tiers/Nope", json={"provider": "p1"}).status_code == 422
    assert client.put("/api/config/tiers/Basic", json={"provider": "ghost"}).status_code == 422


def test_smart_router_toggle(tmp_path: Path) -> None:
    client = _client(tmp_path)
    on = client.post("/api/config/smart-router", json={"enabled": True})
    assert on.status_code == 200
    assert on.json()["smart_router_enabled"] is True
    off = client.post("/api/config/smart-router", json={"enabled": False})
    assert off.json()["smart_router_enabled"] is False


# ---------- 桌面端运行时白名单授权 ----------


def test_grant_root_extends_workspace_for_new_projects(tmp_path: Path) -> None:
    client = _client(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    project_root = outside / "proj"
    project_root.mkdir()

    # 未授权：白名单之外的路径必须被拒绝
    denied = client.post("/api/projects", json={"name": "P", "root_path": str(project_root)})
    assert denied.status_code == 422

    granted = client.post("/api/desktop/roots", json={"path": str(outside)})
    assert granted.status_code == 200
    assert str(outside.resolve()) in granted.json()["allowed_roots"]

    created = client.post("/api/projects", json={"name": "P", "root_path": str(project_root)})
    assert created.status_code == 201


def test_grant_root_rejects_missing_or_non_directory(tmp_path: Path) -> None:
    client = _client(tmp_path)

    missing = client.post("/api/desktop/roots", json={"path": str(tmp_path / "nope")})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_workspace_root"

    a_file = tmp_path / "a.txt"
    a_file.write_text("x", encoding="utf-8")
    assert client.post("/api/desktop/roots", json={"path": str(a_file)}).status_code == 422


# ---------- 令牌鉴权覆盖 REST ----------


def test_rest_endpoints_require_token_when_configured(tmp_path: Path) -> None:
    client = _client(tmp_path, ws_auth_token=TOKEN)
    auth = {"Authorization": f"Bearer {TOKEN}"}

    for method, path in (
        ("get", "/api/config"),
        ("get", "/api/desktop/info"),
        ("get", "/api/projects"),
        ("get", "/api/notes?scope=global"),
    ):
        assert getattr(client, method)(path).status_code == 401, path
        assert getattr(client, method)(path, headers=auth).status_code == 200, path

    assert client.post("/api/config/providers", json=_provider_payload()).status_code == 401
    assert client.post("/api/desktop/roots", json={"path": str(tmp_path)}).status_code == 401

    # 401 也应带上可追踪的 request id
    rejected = client.get("/api/config")
    assert rejected.json()["error"]["request_id"]
    assert rejected.json()["error"]["code"] == "not_authenticated"


def test_healthz_stays_open_for_desktop_readiness_poll(tmp_path: Path) -> None:
    client = _client(tmp_path, ws_auth_token=TOKEN)
    assert client.get("/healthz").status_code == 200


# ---------- 数据目录统一 ----------


def test_access_log_redacts_token_query_param() -> None:
    """WebSocket 令牌只能走查询参数，访问日志必须脱敏（日志常被当附件外发）。"""
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        "WebSocket %s [accepted]",
        ("/api/ws/projects/p/sessions/s?token=deadbeefcafe",),
        None,
    )
    RedactTokenFilter().filter(record)

    rendered = record.getMessage()
    assert "deadbeefcafe" not in rendered
    assert "token=***" in rendered


def test_access_log_redaction_keeps_other_query_params() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '"GET %s HTTP/1.1" %s',
        ("/api/notes?scope=global&token=secret123&limit=5", 200),
        None,
    )
    RedactTokenFilter().filter(record)

    rendered = record.getMessage()
    assert "secret123" not in rendered
    assert "scope=global" in rendered
    assert "limit=5" in rendered


def test_config_manager_honors_user_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "appdata"
    monkeypatch.setenv("ROUTIVUS_USER_DIR", str(target))

    manager = ConfigManager(load_env=False)
    assert manager.user_dir == target
    assert manager.user_config_path == target / "config.json"

    # 显式参数优先于环境变量
    explicit = ConfigManager(user_dir=tmp_path / "explicit", load_env=False)
    assert explicit.user_dir == tmp_path / "explicit"


def test_server_config_reads_user_dir_from_env(tmp_path: Path) -> None:
    config = ServerConfig.from_env(
        {
            "ROUTIVUS_USER_DIR": str(tmp_path / "appdata"),
            "ROUTIVUS_WORKSPACE_ROOTS": str(tmp_path),
        }
    )
    assert config.user_dir == tmp_path / "appdata"
    assert config.projects_file == tmp_path / "appdata" / "projects.json"
    assert config.database_path == tmp_path / "appdata" / "workspace.sqlite3"
