"""Phase 1 Web server and project registry tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig


def _client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    return TestClient(create_app(registry=registry)), workspace, tmp_path


def test_health_check_returns_request_id() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz", headers={"X-Request-ID": "req-test-1"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "routivus-server",
        "version": "0.1.0",
        "request_id": "req-test-1",
    }
    assert response.headers["X-Request-ID"] == "req-test-1"


def test_websocket_implementation_is_installed() -> None:
    """回归：uvicorn 只声明 http 栈，WebSocket 协议得有实现（websockets / wsproto）。

    缺了它每个升级请求都被 uvicorn 拒掉（日志：Unsupported upgrade request /
    No supported WebSocket library detected），前端表现是**永远"重连中"**——对话
    通道整条不可用。而 REST 全绿、所有走 TestClient 的用例也全绿（TestClient 自带
    进程内 WS 实现），所以这个缺口必须从依赖层面钉住，否则很容易再被漏掉。
    """
    from uvicorn.config import Config

    config = Config(app=None, ws="auto")
    config.load()

    assert config.ws_protocol_class is not None, (
        "缺少 WebSocket 实现：装 websockets（uv add websockets / uv sync）"
    )


def test_server_config_uses_safe_default_port_and_validates_override(tmp_path: Path) -> None:
    config = ServerConfig.from_env(
        {
            "ROUTIVUS_USER_DIR": str(tmp_path / "user"),
            "ROUTIVUS_WORKSPACE_ROOTS": str(tmp_path),
        }
    )
    assert config.host == "127.0.0.1"
    assert config.port == 18765

    custom = ServerConfig.from_env(
        {
            "ROUTIVUS_USER_DIR": str(tmp_path / "user"),
            "ROUTIVUS_WORKSPACE_ROOTS": str(tmp_path),
            "ROUTIVUS_SERVER_HOST": "localhost",
            "ROUTIVUS_SERVER_PORT": "19001",
        }
    )
    assert (custom.host, custom.port) == ("localhost", 19001)

    invalid = ServerConfig.from_env(
        {
            "ROUTIVUS_USER_DIR": str(tmp_path / "user"),
            "ROUTIVUS_WORKSPACE_ROOTS": str(tmp_path),
            "ROUTIVUS_SERVER_PORT": "65536",
        }
    )
    assert invalid.port == 18765


def test_project_crud_does_not_mutate_project_directory(tmp_path: Path) -> None:
    client, workspace, _ = _client(tmp_path)
    project_root = workspace / "demo"
    project_root.mkdir()
    marker = project_root / "README.md"
    marker.write_text("# demo\n", encoding="utf-8")

    created = client.post(
        "/api/projects",
        json={"name": "Demo", "root_path": str(project_root)},
    )
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "Demo"
    assert Path(project["root_path"]) == project_root.resolve()
    assert project["status"] == "idle"
    assert project["stats"] == {"sessions": 0, "notes": 0, "calls_today": 0}

    project_id = project["id"]
    renamed = client.patch(f"/api/projects/{project_id}", json={"name": "Demo 2"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Demo 2"

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [project_id]

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert marker.read_text(encoding="utf-8") == "# demo\n"


def test_session_rename_only_touches_title(tmp_path: Path) -> None:
    """会话重命名（`PATCH /api/sessions/{id}`）只改标题。

    这条路径此前没有测试覆盖，而左侧会话列表右键的「重命名会话」就打在它上面：
    改完必须能从会话详情与列表里都读到新标题（不是只改了返回值），且 id /
    project_id / status 原样——重命名不该顺带改任何别的状态。
    """
    client, workspace, _ = _client(tmp_path)
    project_root = workspace / "demo"
    project_root.mkdir()
    project = client.post(
        "/api/projects", json={"name": "Demo", "root_path": str(project_root)}
    ).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "新建会话"}
    ).json()

    renamed = client.patch(f"/api/sessions/{session['id']}", json={"title": "登录重构"})

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "登录重构"
    assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "登录重构"
    listed = client.get(f"/api/projects/{project['id']}/sessions").json()
    assert [item["title"] for item in listed] == ["登录重构"]
    assert renamed.json()["id"] == session["id"]
    assert renamed.json()["project_id"] == session["project_id"]
    assert renamed.json()["status"] == session["status"]


@pytest.mark.parametrize("title", ["", "x" * 201])
def test_session_rename_rejects_empty_or_overlong_title(tmp_path: Path, title: str) -> None:
    """空标题与超 200 字都要被拒：前端的 maxLength 只是顺手挡一下，不是唯一防线。"""
    client, workspace, _ = _client(tmp_path)
    project_root = workspace / "demo"
    project_root.mkdir()
    project = client.post(
        "/api/projects", json={"name": "Demo", "root_path": str(project_root)}
    ).json()
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": "新建会话"}
    ).json()

    response = client.patch(f"/api/sessions/{session['id']}", json={"title": title})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    # 被拒之后原标题原样留着，不会被写坏
    assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "新建会话"


def test_remove_project_blocked_while_session_running(tmp_path: Path) -> None:
    """移除只摘注册表：运行中的会话先拦住，移除后数据仍可访问。"""
    client, workspace, _ = _client(tmp_path)
    project_root = workspace / "demo"
    project_root.mkdir()
    project = client.post(
        "/api/projects", json={"name": "Demo", "root_path": str(project_root)}
    ).json()
    session = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"}).json()

    class _PendingTask:
        def done(self) -> bool:
            return False

    client.app.state.running_tasks[session["id"]] = _PendingTask()
    blocked = client.delete(f"/api/projects/{project['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "project_busy"

    client.app.state.running_tasks.pop(session["id"], None)
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    # 数据保留：会话仍可读取（项目只是从列表里摘掉）
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200


def test_project_path_must_be_inside_allowed_workspace(tmp_path: Path) -> None:
    client, workspace, root = _client(tmp_path)
    outside = root / "outside"
    outside.mkdir()

    response = client.post(
        "/api/projects",
        json={"name": "Outside", "root_path": str(outside)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_project_path"


def test_duplicate_project_path_and_missing_project_use_structured_errors(tmp_path: Path) -> None:
    client, workspace, _ = _client(tmp_path)
    project_root = workspace / "demo"
    project_root.mkdir()
    payload = {"name": "Demo", "root_path": str(project_root)}

    assert client.post("/api/projects", json=payload).status_code == 201
    duplicate = client.post("/api/projects", json={**payload, "name": "Other"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "project_already_exists"

    missing = client.get("/api/projects/not-found")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "project_not_found"
    assert missing.headers["X-Request-ID"]


def test_request_validation_is_structured(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    response = client.post(
        "/api/projects",
        json={"name": "", "root_path": "x", "unexpected": True},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]
    assert body["error"]["details"]
