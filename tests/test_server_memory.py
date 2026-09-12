"""Web Console 长期记忆视图（会话快照 memory 段 + GET /api/sessions/{id}/memory）。

验的是**服务端接线**：项目级记忆库的真实条目、库不存在时的只读降级、命令改动后
的刷新信号。记忆库本身的读写语义由 tests/test_memory* 覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from routivus.config.manager import ConfigManager
from routivus.config.settings import load_settings
from routivus.memory.manager import MemoryManager
from routivus.memory.store import SQLiteMemoryStore
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


def _client(tmp_path: Path, factory: Any = None) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    return TestClient(
        create_app(
            registry=registry,
            store=WorkspaceStore(tmp_path / "workspace.sqlite3"),
            config=config,
            agent_factory=factory or (lambda project, session: object()),
        )
    )


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir(exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def _memory_db(project: dict) -> Path:
    return Path(project["root_path"]) / ".routivus" / "memory.db"


def _seed(project: dict, *contents: str) -> None:
    store = SQLiteMemoryStore(_memory_db(project))
    for content in contents:
        store.save(content)


def _memory_path(session: dict) -> str:
    return f"/api/sessions/{session['id']}/memory"


# ==========================================================================
# 端点的读取与降级
# ==========================================================================


def test_endpoint_returns_saved_entries(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "约定：提交信息用中文", "偏好：表格用 markdown")
    session = _session(client, project)

    response = client.get(_memory_path(session))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "available"
    assert payload["scope"] == "project"
    assert payload["count"] == 2
    assert {item["content"] for item in payload["items"]} == {
        "约定：提交信息用中文",
        "偏好：表格用 markdown",
    }
    entry = payload["items"][0]
    assert entry["id"] > 0
    assert entry["updated_at"]
    assert entry["source"] == "manual"


def test_endpoint_does_not_create_db_for_untouched_project(tmp_path: Path) -> None:
    """只看一眼记忆不该在用户的项目里凭空建库（读接口不能有写副作用）。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    payload = client.get(_memory_path(session)).json()
    assert payload["status"] == "available"
    assert payload["count"] == 0
    assert payload["items"] == []
    assert not _memory_db(project).exists()


def test_endpoint_unknown_session_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/sessions/missing/memory").status_code == 404


def test_endpoint_reports_unavailable_store(tmp_path: Path) -> None:
    """库文件损坏时降级成空视图，不把 500 抛给界面。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    db = _memory_db(project)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("not a sqlite database", encoding="utf-8")

    response = client.get(_memory_path(session))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["items"] == []
    assert payload["error"]


# ==========================================================================
# 会话快照
# ==========================================================================


def test_snapshot_carries_memory_items(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "约定：不要动 migrations")
    session = _session(client, project)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        snapshot = socket.receive_json()

    assert snapshot["type"] == "session.snapshot"
    memory = snapshot["data"]["memory"]
    assert memory["count"] == 1
    assert [item["content"] for item in memory["items"]] == ["约定：不要动 migrations"]


def test_memory_view_reuses_agent_instance(tmp_path: Path) -> None:
    """有 agent 时直接读它已打开的库，不再为只读视图另开一份句柄。

    这保证「命令写入」与「界面读取」看到的一定是同一份数据（project_memories
    作为 agent 缺席时的兜底缓存，此时应保持为空）。
    """
    client = _client(tmp_path, factory=lambda project, session: _MemoryAgent(Path(project.root_path)))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()  # snapshot
        socket.send_json({"type": "user_message", "content": "/save 走 agent 实例", "request_id": "r1"})
        for _ in range(30):
            if socket.receive_json().get("type") == "memory.updated":
                break

    assert session["id"] in client.app.state.session_agents
    payload = client.get(_memory_path(session)).json()
    assert payload["count"] == 1
    assert client.app.state.project_memories == {}


# ==========================================================================
# 命令副作用 → 刷新信号
# ==========================================================================


class _MemoryAgent:
    """带真实 MemoryManager 的替身 agent：/save 直接落到项目记忆库。"""

    def __init__(self, root: Path) -> None:
        manager = ConfigManager(
            user_dir=root / ".routivus-test-ud",
            project_dir=root / ".routivus",
            load_env=False,
        )
        manager.upsert_provider(
            "prov",
            {
                "api_base": "http://127.0.0.1:9",
                "default_model": "m1",
                "api_key": "sk-test",
                "models": ["m2"],
            },
        )
        manager.set_active("prov", "m1")
        self.llm = object()
        self.tools = object()
        self.settings = load_settings(manager)
        self.config_manager = manager
        self.approval_policy = None
        self.memory_manager = MemoryManager(root)

    def clear(self) -> None:
        return None


def test_save_command_persists_and_signals_refresh(tmp_path: Path) -> None:
    client = _client(tmp_path, factory=lambda project, session: _MemoryAgent(Path(project.root_path)))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()  # snapshot
        socket.send_json({"type": "user_message", "content": "/save 约定：中文注释", "request_id": "r1"})
        received: list[dict] = []
        for _ in range(30):
            event = socket.receive_json()
            received.append(event)
            if event.get("type") == "memory.updated":
                break

    assert any(event.get("type") == "command.executed" and event["data"]["ok"] for event in received)
    notices = [event["data"] for event in received if event.get("type") == "memory.updated"]
    assert notices, "记忆命令后应推送 memory.updated，供侧栏重拉条目"
    assert notices[0]["kind"] == "memory.command"

    payload = client.get(_memory_path(session)).json()
    assert payload["count"] == 1
    assert payload["items"][0]["content"] == "约定：中文注释"
