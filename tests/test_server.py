"""Phase 1 Web server and project registry tests."""

from __future__ import annotations

from pathlib import Path

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
