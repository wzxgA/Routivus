"""会话时间线段落（思考 / 正文 / 工具）与快照回看窗口的协议级测试（方案 07）。

覆盖四类行为：

1. **段落切分**：thinking / content / tool_call 的边界处各落一条段落消息，
   并广播 `message.segment`（先落库、后发事件）。
2. **来源分桶**：/plan 子任务（并行 worker 同理）的事件按来源各自成段，
   不同来源的文字不允许黏进同一段（07 §4.2）。
3. **`message.completed` 语义收窄**：不再携带整段正文（07 §4.3）。
4. **回看窗口**：消息取"最近 N 条"、卡片回放按类型取"最近 N 张"，
   事件表被 `message.delta` 灌满后最新卡片仍可回放（07 §4.6b/§4.7）。

真实 LLM / 执行器不参与：agent 与 PlanExecutor 都用脚本化替身，验的是服务端接线。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.plan import Plan, PlanEvent, PlanTask
from routivus.agent.react import AgentEvent
from routivus.llm.types import ToolCall
from routivus.server import ProjectRegistry, create_app
from routivus.server.app import MAX_THINKING_SEGMENT_CHARS, _replay_card_events
from routivus.server.config import ServerConfig
from routivus.server.storage import EventRecord, WorkspaceStore


# ==========================================================================
# 替身
# ==========================================================================


class _ScriptedAgent:
    """按脚本回放 AgentEvent 序列的假 agent（够 llm / tools / settings 三个依赖）。"""

    def __init__(self, script: list[AgentEvent]) -> None:
        self._script = script
        self.llm = object()
        self.tools = object()
        self.settings = object()
        self.approval_policy = None
        self.ask_requester = None

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        for event in self._script:
            yield event
        yield AgentEvent(kind="done")


def _plan(goal: str) -> Plan:
    tasks = [
        PlanTask("t1", "拆分配置", "读取配置", []),
        PlanTask("t2", "落地改动", "写文件", []),
    ]
    return Plan(goal=goal, tasks=tasks, batches=[["t1"], ["t2"]])


class _ScriptedPlanExecutor:
    """替身 PlanExecutor：按脚本交替下发两个子任务的事件（模拟并发交错）。"""

    instances: list["_ScriptedPlanExecutor"] = []
    script: list[PlanEvent] = []

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")

    async def run(self, goal: str) -> AsyncIterator[PlanEvent]:
        plan = _plan(goal)
        yield PlanEvent(kind="plan_generated", plan=plan, message="2 个子任务")
        yield PlanEvent(kind="review", plan=plan, message="等待用户审阅")
        assert self.reviewer is not None
        decision = await self.reviewer(plan)
        if decision.action != "execute":
            yield PlanEvent(kind="cancelled", plan=plan, message="用户取消计划")
            return
        yield PlanEvent(kind="approved", plan=plan, message="计划已批准")
        for event in _ScriptedPlanExecutor.script:
            yield event
        yield PlanEvent(kind="plan_done", plan=plan, message="计划完成")


@pytest.fixture(autouse=True)
def _patch_plan_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    _ScriptedPlanExecutor.instances = []
    _ScriptedPlanExecutor.script = []
    monkeypatch.setattr("routivus.agent.plan.PlanExecutor", _ScriptedPlanExecutor)


# ==========================================================================
# 脚手架（与 test_server_plan_team 同构）
# ==========================================================================


def _client(tmp_path: Path, agent_factory: Any) -> TestClient:
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
            agent_factory=agent_factory,
        )
    )


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir()
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def _receive_until(socket: Any, predicate: Any, limit: int = 120) -> list[dict]:
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


def _cards(events: list[dict], event_type: str) -> list[dict]:
    return [item["data"] for item in events if item.get("type") == event_type]


def _completed(event: dict) -> bool:
    return event.get("type") == "session.status" and event.get("data", {}).get("status") == "completed"


# ==========================================================================
# 段落切分与 message.segment
# ==========================================================================


def test_segments_split_by_kind_and_tool(tmp_path: Path) -> None:
    """thinking → 文A → 工具 → 文B 产生 3 条段落消息，工具卡插在两段正文之间。"""
    script = [
        AgentEvent(kind="thinking", text="先想一下"),
        AgentEvent(kind="content", text="文A"),
        AgentEvent(kind="tool_call", tool_call=ToolCall(id="c1", name="read_file", arguments="{}")),
        AgentEvent(kind="content", text="文B"),
    ]
    client = _client(tmp_path, lambda project, session: _ScriptedAgent(script))
    project = _project(client)
    session = _session(client, project)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()  # session.snapshot
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        events = _receive_until(socket, _completed)

    segments = _cards(events, "message.segment")
    assert [(item["message"]["role"], item["message"]["content"]) for item in segments] == [
        ("thinking", "先想一下"),
        ("assistant", "文A"),
        ("assistant", "文B"),
    ]
    # 主 ReAct 轮来源为空串
    assert [item["source"] for item in segments] == ["", "", ""]
    # 顺序：思考段与"文A"段在工具卡之前，"文B"段在工具卡之后
    types = [item["type"] for item in events]
    segment_indexes = [i for i, t in enumerate(types) if t == "message.segment"]
    tool_index = types.index("tool.started")
    assert segment_indexes[0] < tool_index and segment_indexes[1] < tool_index
    assert segment_indexes[2] > tool_index

    # 落库：thinking 成为独立 role，且 REST 可读回（顺序与事件一致）
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages] == ["user", "thinking", "assistant", "assistant"]


def test_message_completed_carries_no_content(tmp_path: Path) -> None:
    """`message.completed` 只作"本轮结束"信号，不再携带整段正文（07 §4.3）。"""
    script = [AgentEvent(kind="content", text="只有一段")]
    client = _client(tmp_path, lambda project, session: _ScriptedAgent(script))
    project = _project(client)
    session = _session(client, project)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        events = _receive_until(socket, _completed)

    completed = _cards(events, "message.completed")
    assert completed and "message" not in completed[0]


def test_whitespace_only_segment_notifies_frontend(tmp_path: Path) -> None:
    """空白段不落库，但仍要发收尾事件。

    否则前端那条活跃段永远收不掉：界面上会永久留一个空块（看起来就是一条横线），
    且此后不再有任何事件能让它消失。
    """
    script = [AgentEvent(kind="content", text="  \n  ")]
    client = _client(tmp_path, lambda project, session: _ScriptedAgent(script))
    project = _project(client)
    session = _session(client, project)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        events = _receive_until(socket, _completed)

    segments = _cards(events, "message.segment")
    assert len(segments) == 1
    assert segments[0]["message"] is None
    assert segments[0]["source"] == ""
    # 空白段不落库：消息表里只有用户那条
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages] == ["user"]


def test_thinking_segment_truncated(tmp_path: Path) -> None:
    script = [AgentEvent(kind="thinking", text="想" * (MAX_THINKING_SEGMENT_CHARS + 500))]
    client = _client(tmp_path, lambda project, session: _ScriptedAgent(script))
    project = _project(client)
    session = _session(client, project)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        events = _receive_until(socket, _completed)

    segments = _cards(events, "message.segment")
    assert len(segments) == 1
    content = segments[0]["message"]["content"]
    assert len(content) == MAX_THINKING_SEGMENT_CHARS + len("…（思考过长已截断）")
    assert content.endswith("…（思考过长已截断）")


# ==========================================================================
# 来源分桶（并行 worker）
# ==========================================================================


def test_parallel_sources_keep_segments_apart(tmp_path: Path) -> None:
    """两个子任务的 delta 交替到达：各自成段，不黏成一段（07 §4.2）。"""
    plan = _plan("goal")
    _ScriptedPlanExecutor.script = [
        PlanEvent(kind="subtask_event", plan=plan, task=plan.tasks[0],
                  agent_event=AgentEvent(kind="thinking", text="想A")),
        PlanEvent(kind="subtask_event", plan=plan, task=plan.tasks[1],
                  agent_event=AgentEvent(kind="content", text="B1")),
        PlanEvent(kind="subtask_event", plan=plan, task=plan.tasks[0],
                  agent_event=AgentEvent(kind="content", text="A1")),
        PlanEvent(kind="subtask_event", plan=plan, task=plan.tasks[1],
                  agent_event=AgentEvent(kind="content", text="B2")),
    ]
    client = _client(tmp_path, lambda project, session: _ScriptedAgent([]))
    project = _project(client)
    session = _session(client, project)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan goal"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        review = events[-1]["data"]
        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review["review_id"],
            "action": "execute",
        })
        events += _receive_until(socket, _completed)

    segments = _cards(events, "message.segment")
    by_source = {(item["source"], item["message"]["role"]): item["message"]["content"] for item in segments}
    assert by_source[("task:t1", "thinking")] == "想A"
    assert by_source[("task:t1", "assistant")] == "A1"
    assert by_source[("task:t2", "assistant")] == "B1B2"  # t2 自己的两段正文合一段
    # 关键断言：A 与 B 从不共享同一段（单桶实现会得到 "想AB1" / "A1B2" 这类黏连）


# ==========================================================================
# 回看窗口与回放载荷
# ==========================================================================


def test_list_recent_messages_keeps_newest(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    for index in range(600):
        store.add_message(session.id, "assistant", f"m{index}")
    records = store.list_recent_messages(session.id, limit=500)
    assert len(records) == 500
    assert records[0].content == "m100"  # 最旧的被裁掉，最新的不能丢
    assert records[-1].content == "m599"


def test_list_card_events_ignores_delta_volume(tmp_path: Path) -> None:
    """事件表被 1500 条 message.delta 灌满后，最新 tool.started 仍在窗口内（07 §4.6b）。"""
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    for _ in range(1500):
        store.append_event(session.id, session.project_id, "message.delta", {"text": "x"})
    late = store.append_event(session.id, session.project_id, "tool.started", {"tool_call_id": "c9"})
    events = store.list_card_events(session.id, ("tool.started", "tool.completed"), limit=1000)
    assert [item.event_id for item in events] == [late.event_id]
    # 全量计数不受窗口影响
    assert store.count_events(session.id, "message.delta") == 1500


def test_replay_card_events_carry_occurred_at() -> None:
    """回放条目带 occurred_at：前端按它把 messages 与 replay 归并（07 §4.6a）。"""
    record = EventRecord("e1", 1, "s1", "p1", "tool.started", {"ok": True}, "2026-01-01T00:00:00+00:00")
    replay = _replay_card_events([record])
    assert replay == [{
        "type": "tool.started",
        "sequence": 1,
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "data": {"ok": True},
    }]
