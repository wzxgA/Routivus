"""Phase 2 server integration tests."""

from __future__ import annotations

import types
from pathlib import Path

from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


def _app(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    return TestClient(create_app(registry=registry, store=store)), workspace, tmp_path


def _project(client: TestClient, workspace: Path, name: str) -> dict:
    root = workspace / name.lower()
    root.mkdir()
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def test_global_notes_include_project_notes_but_project_notes_are_isolated(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project_a = _project(client, workspace, "Alpha")
    project_b = _project(client, workspace, "Beta")

    global_note = client.post("/api/notes", json={"title": "全局", "body_markdown": "所有项目可见"})
    note_a = client.post(f"/api/projects/{project_a['id']}/notes", json={"title": "Alpha 笔记", "body_markdown": "A"})
    note_b = client.post(f"/api/projects/{project_b['id']}/notes", json={"title": "Beta 笔记", "body_markdown": "B"})
    assert (global_note.status_code, note_a.status_code, note_b.status_code) == (201, 201, 201)

    all_notes = client.get("/api/notes?scope=global").json()
    assert {item["title"] for item in all_notes} == {"全局", "Alpha 笔记", "Beta 笔记"}
    assert [item["project_name"] for item in all_notes if item["title"] == "全局"] == [None]

    alpha_notes = client.get(f"/api/projects/{project_a['id']}/notes").json()
    assert [item["title"] for item in alpha_notes] == ["Alpha 笔记"]

    projects = {item["name"]: item for item in client.get("/api/projects").json()}
    assert projects["Alpha"]["stats"]["notes"] == 1
    assert projects["Beta"]["stats"]["notes"] == 1


def test_note_search_paging_pin_stats_and_optimistic_lock(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project = _project(client, workspace, "Alpha")
    other_project = _project(client, workspace, "Beta")
    first = client.post(f"/api/projects/{project['id']}/notes", json={"title": "First", "body_markdown": "searchable text", "tags": ["one", "one"]}).json()
    second = client.post(f"/api/projects/{project['id']}/notes", json={"title": "Second", "body_markdown": "other", "tags": ["two"]}).json()
    foreign = client.post(f"/api/projects/{other_project['id']}/notes", json={"title": "Foreign", "body_markdown": "private"}).json()
    client.post("/api/notes", json={"title": "Global", "body_markdown": "workspace"})

    assert [item["title"] for item in client.get(f"/api/projects/{project['id']}/notes?query=searchable").json()] == ["First"]
    page = client.get(f"/api/projects/{project['id']}/notes?limit=1&cursor=1").json()
    assert len(page) == 1
    assert page[0]["title"] in {"First", "Second"}

    pinned = client.post(f"/api/projects/{project['id']}/notes/{first['id']}/pin", json={"pinned": True})
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True

    stats = client.get("/api/notes/stats").json()
    assert stats == {"total": 4, "global_notes": 1, "project_notes": 3, "by_project": {project["id"]: 2, other_project["id"]: 1}}

    updated = client.patch(f"/api/notes/{first['id']}", json={"body_markdown": "changed", "version": first["version"] + 1}).json()
    assert updated["body_markdown"] == "changed"
    conflict = client.patch(f"/api/notes/{first['id']}", json={"body_markdown": "stale", "version": first["version"]})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "note_conflict"

    foreign_read = client.get(f"/api/projects/{project['id']}/notes/{foreign['id']}")
    foreign_edit = client.patch(f"/api/projects/{project['id']}/notes/{foreign['id']}", json={"title": "edited"})
    foreign_delete = client.delete(f"/api/projects/{project['id']}/notes/{foreign['id']}")
    assert (foreign_read.status_code, foreign_edit.status_code, foreign_delete.status_code) == (404, 404, 404)


def test_sessions_messages_and_project_ownership(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project_a = _project(client, workspace, "Alpha")
    project_b = _project(client, workspace, "Beta")

    created = client.post(f"/api/projects/{project_a['id']}/sessions", json={"title": "Build"})
    assert created.status_code == 201
    session = created.json()
    assert client.get(f"/api/sessions/{session['id']}").json()["project_id"] == project_a["id"]
    assert client.get(f"/api/projects/{project_b['id']}/sessions").json() == []

    mismatch = client.get(f"/api/projects/{project_b['id']}/sessions/{session['id']}")
    assert mismatch.status_code == 404

    store = client.app.state.workspace_store
    message = store.add_message(session["id"], "user", "hello")
    history = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert history[0]["id"] == message.id
    assert history[0]["content"] == "hello"


def test_websocket_snapshot_message_and_error_events(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project = _project(client, workspace, "Alpha")
    session = client.post(f"/api/projects/{project['id']}/sessions", json={}).json()

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        snapshot = socket.receive_json()
        assert snapshot["type"] == "session.snapshot"
        assert {"event_id", "sequence", "session_id", "project_id", "occurred_at", "data"} <= snapshot.keys()
        assert snapshot["data"]["session"]["id"] == session["id"]

        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        event_types = []
        for _ in range(5):
            event = socket.receive_json()
            event_types.append(event["type"])
            if event["type"] == "session.status" and event["data"].get("status") == "failed":
                break
        assert "message.created" in event_types
        assert "error" in event_types
        assert event_types[-1] == "session.status"


def test_websocket_agent_events_are_persisted(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project = _project(client, workspace, "Alpha")
    session = client.post(f"/api/projects/{project['id']}/sessions", json={}).json()

    class FakeAgent:
        async def run(self, content: str):
            yield AgentEvent(kind="content", text=f"reply: {content}")
            yield AgentEvent(kind="done")

    app = client.app
    app.state.agent_factory = lambda _project, _session: FakeAgent()
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "hello"})
        events = []
        for _ in range(6):
            event = socket.receive_json()
            events.append(event)
            if event["type"] == "session.status" and event["data"].get("status") == "completed":
                break
    assert any(event["type"] == "message.delta" and event["data"]["text"] == "reply: hello" for event in events)
    assert client.get(f"/api/sessions/{session['id']}/messages").json()[-1]["content"] == "reply: hello"


def test_websocket_request_id_is_idempotent(tmp_path: Path) -> None:
    client, workspace, _ = _app(tmp_path)
    project = _project(client, workspace, "Alpha")
    session = client.post(f"/api/projects/{project['id']}/sessions", json={}).json()

    class FakeAgent:
        async def run(self, content: str):
            yield AgentEvent(kind="content", text="ok")
            yield AgentEvent(kind="done")

    client.app.state.agent_factory = lambda _project, _session: FakeAgent()
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "same", "content": "first"})
        for _ in range(6):
            event = socket.receive_json()
            if event["type"] == "session.status" and event["data"].get("status") == "completed":
                break
        socket.send_json({"type": "user_message", "request_id": "same", "content": "second"})
        duplicate = socket.receive_json()
        assert duplicate["type"] == "error"
        assert duplicate["data"]["code"] == "duplicate_request"

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages] == ["user", "assistant"]


def test_websocket_token_authentication(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        database_path=tmp_path / "workspace.sqlite3",
        ws_auth_token="secret-token",
    )
    client = TestClient(create_app(registry=registry, config=config))
    project_root = workspace / "alpha"
    project_root.mkdir()

    auth = {"Authorization": "Bearer secret-token"}

    # 配置令牌后 REST 同样受保护：此前只有 WebSocket 校验，HTTP 侧完全裸奔，
    # 本机任意进程都能注册项目、驱动 Agent 工具并自行批准高危调用。
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers=auth).status_code == 200
    # ?token= 供浏览器 / WebSocket 场景使用
    assert client.get("/api/projects?token=secret-token").status_code == 200
    # /healthz 必须免鉴权：桌面壳在拿到令牌之前就要轮询它就绪
    assert client.get("/healthz").status_code == 200

    project = client.post(
        "/api/projects", json={"name": "Alpha", "root_path": str(project_root)}, headers=auth
    ).json()
    session = client.post(f"/api/projects/{project['id']}/sessions", json={}, headers=auth).json()

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        assert socket.receive_json()["code"] == "not_authenticated"

    with client.websocket_connect(
        f"/api/ws/projects/{project['id']}/sessions/{session['id']}",
        headers={"Authorization": "Bearer secret-token"},
    ) as socket:
        assert socket.receive_json()["type"] == "session.snapshot"


# ==========================================================================
# 会话删除
# ==========================================================================


def _new_session(client: TestClient, workspace: Path, title: str = "待删") -> tuple[dict, dict]:
    """建一个项目 + 一个会话（每个用例各自一个 tmp_path，所以项目名可以固定）。"""
    project = _project(client, workspace, "Alpha")
    session = client.post(
        f"/api/projects/{project['id']}/sessions", json={"title": title}
    ).json()
    return project, session


def test_delete_session_cascades_messages_and_events(tmp_path: Path) -> None:
    """删会话要把消息与事件一起清掉（外键级联），而不是留下孤儿数据。"""
    client, workspace, _ = _app(tmp_path)
    project, session = _new_session(client, workspace)
    store = client.app.state.workspace_store
    store.add_message(session["id"], "user", "hi")
    store.append_event(session["id"], project["id"], "note.created", {"x": 1})
    assert len(store.list_messages(session["id"])) == 1
    assert store.count_events(session["id"], "note.created") == 1

    response = client.delete(f"/api/sessions/{session['id']}")

    assert response.status_code == 204
    assert store.get_session(session["id"]) is None
    assert store.list_messages(session["id"]) == []
    assert store.count_events(session["id"], "note.created") == 0
    assert store.list_sessions(project["id"]) == []
    assert client.get(f"/api/sessions/{session['id']}").status_code == 404


def test_delete_session_rejects_running_then_clears_memory_state(tmp_path: Path) -> None:
    """运行中拒绝删除；删成功后顺手丢掉进程内状态（agent / 审批 / 审阅 / 路由器）。"""
    client, workspace, _ = _app(tmp_path)
    _, session = _new_session(client, workspace, "运行中")
    app = client.app
    app.state.running_tasks[session["id"]] = types.SimpleNamespace(done=lambda: False)

    busy = client.delete(f"/api/sessions/{session['id']}")

    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "session_busy"
    assert app.state.workspace_store.get_session(session["id"]) is not None

    # 任务结束后可删；内存态也应当被清掉，否则这些字典会随删会话一直涨
    app.state.running_tasks.pop(session["id"])
    app.state.session_agents[session["id"]] = object()
    app.state.session_approvals[session["id"]] = object()
    app.state.session_reviews[session["id"]] = object()
    app.state.session_routers[session["id"]] = None

    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    for store_name in (
        "session_agents",
        "session_approvals",
        "session_reviews",
        "session_routers",
    ):
        assert session["id"] not in getattr(app.state, store_name), store_name


def test_delete_unknown_session_is_404(tmp_path: Path) -> None:
    client, _, _ = _app(tmp_path)
    response = client.delete("/api/sessions/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"
