"""Web 端 slash 命令通道的协议级测试（plans/enhancement/01-web-slash-commands.md §9）。

验的是**服务端接线**：命令不再被当成聊天发给模型、回执落库并广播、副作用
同步到会话记录与顶栏事件、互斥与排除规则。命令本身的业务语义由
tests/test_cli_commands* 与 service 层测试覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.config.manager import ConfigManager
from routivus.config.settings import load_settings
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


class _CmdAgent:
    """带真实 ConfigManager 的替身 agent：/model、/smartrouter、/clear 走真逻辑。"""

    def __init__(self, manager: ConfigManager) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = load_settings(manager)
        self.config_manager = manager
        self.approval_policy = None
        self.memory_manager = None
        self.cleared = 0
        self.seen: list[str] = []

    def clear(self) -> None:
        self.cleared += 1

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        self.seen.append(content)
        yield AgentEvent(kind="content", text=f"收到：{content}")
        yield AgentEvent(kind="done")


class _PendingTask:
    """占据 running_tasks 的假任务：让命令通道命中互斥规则。"""

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        return None


def _manager(tmp_path: Path) -> ConfigManager:
    manager = ConfigManager(
        user_dir=tmp_path / "ud",
        project_dir=tmp_path / "proj" / ".routivus",
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
    return manager


def _client(tmp_path: Path) -> TestClient:
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
            agent_factory=lambda project, session: _CmdAgent(_manager(tmp_path)),
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


def _ws(project: dict, session: dict) -> str:
    return f"/api/ws/projects/{project['id']}/sessions/{session['id']}"


def _receive_until(socket: Any, predicate: Any, limit: int = 60) -> list[dict]:
    received: list[dict] = []
    for _ in range(limit):
        try:
            event = socket.receive_json()
        except Exception:
            break
        received.append(event)
        if predicate(event):
            break
    return received


def _command_events(events: list[dict]) -> list[dict]:
    return [item["data"] for item in events if item.get("type") == "command.executed"]


def _is_command_event(event: dict) -> bool:
    return event.get("type") == "command.executed"


# ==========================================================================
# 用例
# ==========================================================================


def test_help_command_executes_without_touching_agent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()  # snapshot
        socket.send_json({"type": "user_message", "content": "/help", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)

    data = _command_events(events)[0]
    assert data["ok"] is True
    assert data["command"] == "/help"

    # 回执以 assistant 消息落库；agent.run 未被调用（命令不发给模型）。
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    roles = [(item["role"], item["content"][:20]) for item in messages]
    assert roles[0][0] == "user" and roles[0][1].startswith("/")
    assert messages[-1]["role"] == "assistant"
    agent = client.app.state.session_agents[session["id"]]
    assert agent.seen == []


def test_exit_is_removed_and_session_survives(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/exit", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)
        # /exit 已从命令通道移除：按未知命令处理，绝不触发 service 层的退出语义。
        assert _command_events(events)[0]["ok"] is False
        messages = client.get(f"/api/sessions/{session['id']}/messages").json()
        assert "未知命令" in messages[-1]["content"]

        # 通道未被关闭：后续命令仍可执行。
        socket.send_json({"type": "user_message", "content": "/help", "request_id": "r2"})
        events = _receive_until(socket, _is_command_event)
        assert _command_events(events)[0]["ok"] is True

    updated = client.get(f"/api/sessions/{session['id']}").json()
    assert updated["status"] != "failed"


def test_clear_command_hits_agent_context(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/clear", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)

    assert _command_events(events)[0]["ok"] is True
    agent = client.app.state.session_agents[session["id"]]
    assert agent.cleared == 1
    # 回执说明存储历史不受影响（/clear 在 Web 的语义差异，见方案 §6）。
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert "会话记录仍保留" in messages[-1]["content"]


def test_unknown_command_returns_failure_receipt(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/nope", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)

    assert _command_events(events)[0]["ok"] is False
    # 文案换成 Web 白名单清单：不再出现 /exit /init 等未接入命令（方案 §C）。
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    receipt = messages[-1]["content"]
    assert '未知命令 "/nope"' in receipt
    assert "/plan" in receipt and "/model" in receipt
    assert "/exit" not in receipt and "/init" not in receipt


def test_model_switch_syncs_session_record(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/model m2", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)
        # 副作用事件在 command.executed 之后到达，继续收。
        events += _receive_until(socket, lambda item: item.get("type") == "session.status")
        status_events = [item for item in events if item.get("type") == "session.status"]
        assert status_events, "模型切换后应广播 session.status"

    data = _command_events(events)[0]
    assert data["ok"] is True
    agent = client.app.state.session_agents[session["id"]]
    assert agent.settings.model == "m2"
    updated = client.get(f"/api/sessions/{session['id']}").json()
    assert updated["active_model"] == "m2"
    assert updated["active_provider"] == "prov"


def test_smartrouter_on_emits_router_event(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/smartrouter on", "request_id": "r1"})
        events = _receive_until(socket, _is_command_event)
        # router.updated 在 command.executed 之后到达，继续收。
        events += _receive_until(socket, lambda item: item.get("type") == "router.updated")
        router_events = [item for item in events if item.get("type") == "router.updated"]

    assert _command_events(events)[0]["ok"] is True
    assert router_events and router_events[0]["data"]["enabled"] is True


def test_command_is_rejected_while_turn_running(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    client.app.state.running_tasks[session["id"]] = _PendingTask()

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/clear", "request_id": "r1"})
        events = _receive_until(
            socket,
            lambda item: item.get("type") == "error",
        )

    client.app.state.running_tasks.pop(session["id"], None)
    errors = [item for item in events if item.get("type") == "error"]
    assert errors and errors[0]["data"]["code"] == "session_busy"
    # 被拒绝的命令不应产生回执，也不应创建 agent（惰性创建发生在执行前）。
    assert session["id"] not in client.app.state.session_agents
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


class _StubBridge:
    """只读待决载荷的桥接替身（快照恢复用）。"""

    def __init__(self, payload: dict) -> None:
        self.pending_payload = payload


class _StubResumableTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.title = f"任务 {task_id}"
        self.description = "修复审查未通过"
        self.deps: list[str] = []
        self.status = "needs_input"
        self.pending_repair_scope: list[Any] = []


class _StubResumablePlan:
    def __init__(self) -> None:
        self.goal = "修复登录"
        self.tasks = [_StubResumableTask("t1"), _StubResumableTask("t2")]


class _StubResumableExecutor:
    def __init__(self) -> None:
        self.plan = _StubResumablePlan()
        self.team_id = "team-1"


# ==========================================================================
# 快照恢复（P0/P1/P2）：卡片事件回放 + 待决交互 + Team needs_input
# ==========================================================================


def test_snapshot_replays_cards_and_defaults_pending_empty(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()  # 初次快照
        socket.send_json({"type": "user_message", "content": "/help", "request_id": "r1"})
        _receive_until(socket, _is_command_event)

    # 重连（模拟"重进项目"）：快照必须带回卡片事件，否则命令卡会退化成文字。
    with client.websocket_connect(_ws(project, session)) as socket:
        snapshot = socket.receive_json()["data"]

    replay = snapshot["replay"]
    command_events = [item for item in replay if item["type"] == "command.executed"]
    assert command_events, "快照应回放 command.executed"
    receipt_id = command_events[-1]["data"]["message_id"]
    assert receipt_id, "command.executed 必须携带回执 message_id"

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert receipt_id in {item["id"] for item in messages}
    assert snapshot["pending"] == {}


def test_snapshot_pending_restores_approval_and_review(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    client.app.state.session_approvals[session["id"]] = _StubBridge(
        {"kind": "approval", "approval_id": "ap-1", "tool_name": "shell", "level": "high", "arguments": {}, "timeout": 300}
    )
    client.app.state.session_reviews[session["id"]] = _StubBridge(
        {"kind": "review", "review_id": "rv-1", "mode": "plan", "plan": {"goal": "g", "tasks": [], "batches": []}, "timeout": 300}
    )

    with client.websocket_connect(_ws(project, session)) as socket:
        snapshot = socket.receive_json()["data"]

    assert snapshot["pending"]["approval"]["approval_id"] == "ap-1"
    assert snapshot["pending"]["plan_review"]["review_id"] == "rv-1"


def test_snapshot_surfaces_team_needs_input(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    client.app.state.session_resumable[session["id"]] = ("team", _StubResumableExecutor())

    with client.websocket_connect(_ws(project, session)) as socket:
        snapshot = socket.receive_json()["data"]

    needs_input = [
        item
        for item in snapshot["replay"]
        if item["type"] == "team.updated" and item["data"].get("kind") == "team_needs_input"
    ]
    assert needs_input, "needs_input 的团队任务应在快照里出现"
    payload = needs_input[0]["data"]
    assert "2 个任务" in payload["message"]
    assert [task["id"] for task in payload["plan"]["tasks"]] == ["t1", "t2"]
    assert all(task["status"] == "needs_input" for task in payload["plan"]["tasks"])


def test_cancel_alias_maps_to_cancel_channel(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "/cancel", "request_id": "r1"})
        events = _receive_until(
            socket,
            lambda item: item.get("type") == "session.status"
            and item.get("data", {}).get("status") == "cancelled",
        )

    assert any(
        item.get("type") == "session.status" and item["data"]["status"] == "cancelled"
        for item in events
    )
