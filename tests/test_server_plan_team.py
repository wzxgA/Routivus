"""会话内 `/plan` 与 `/team` 通道的协议级测试。

`PlanExecutor` / `TeamExecutor` 在这里被换成脚本化替身：真实执行器需要 LLM 与
工具注册表，而本文件要验的是**服务端接线** —— 前缀分派、卡片载荷形状（摊平而非
嵌套 `payload`）、审阅往返（execute / cancel / replan / 超时 fail closed）、
`/team resume` 的写入范围路由。真实执行链路由 `tests/test_plan.py`、
`tests/test_team.py` 覆盖。

约定：所有 WS 测试都有硬性 receive 上限，回归时失败而不是挂死。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.plan import Plan, PlanEvent, PlanTask, ReviewDecision
from routivus.agent.react import AgentEvent
from routivus.agent.team import ResourceClaim, TeamEvent, TeamPlan, TeamTask
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


# ==========================================================================
# 替身：脚本化执行器
# ==========================================================================


class _FakeAgent:
    """够用的假 agent：计划 / 团队只需要 llm / tools / settings 三个依赖存在。

    `run()` 供普通对话路径使用，保证「非 /plan、/team 的输入仍走 ReAct」可被验证。
    """

    def __init__(self) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = object()
        self.approval_policy = None
        self.ask_requester = None
        self.seen: list[str] = []

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        self.seen.append(content)
        yield AgentEvent(kind="content", text=f"收到：{content}")
        yield AgentEvent(kind="done")


def _plan(goal: str) -> Plan:
    tasks = [
        PlanTask("t1", "拆分配置", "读取配置", []),
        PlanTask("t2", "落地改动", "写文件", ["t1"]),
    ]
    return Plan(goal=goal, tasks=tasks, batches=[["t1"], ["t2"]])


def _team_plan(goal: str) -> TeamPlan:
    tasks = [
        TeamTask("t1", "改造检索", "替换检索实现", [], owner_role="coder"),
        TeamTask("t2", "回归验证", "跑测试", ["t1"], owner_role="tester"),
    ]
    return TeamPlan(goal=goal, tasks=tasks, batches=[["t1"], ["t2"]])


class _ScriptedPlanExecutor:
    """替身 PlanExecutor：按真实语义走一遍 reviewer 往返与事件顺序。"""

    instances: list["_ScriptedPlanExecutor"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")
        self.decision: ReviewDecision | None = None
        self.resumes: list[str] = []
        _ScriptedPlanExecutor.instances.append(self)

    async def run(self, goal: str) -> AsyncIterator[PlanEvent]:
        plan = _plan(goal)
        yield PlanEvent(kind="plan_generated", plan=plan, message="2 个子任务")
        yield PlanEvent(kind="review", plan=plan, message="等待用户审阅")
        assert self.reviewer is not None
        decision = await self.reviewer(plan)
        self.decision = decision
        if decision.action == "cancel":
            yield PlanEvent(kind="cancelled", plan=plan, message="用户取消计划")
            return
        if decision.action == "replan":
            yield PlanEvent(kind="replanned", plan=plan, message=decision.feedback)
            yield PlanEvent(kind="plan_done", plan=plan, message=f"重新规划：{decision.feedback}")
            return
        yield PlanEvent(kind="approved", plan=plan, message="计划已批准")
        yield PlanEvent(kind="batch_started", plan=plan, batch=["t1"], message="第 1 轮 / 共 2 轮")
        yield PlanEvent(
            kind="subtask_event",
            plan=plan,
            task=plan.tasks[0],
            agent_event=AgentEvent(kind="content", text="正在拆分配置…"),
        )
        plan.tasks[0].status = "done"
        yield PlanEvent(kind="plan_done", plan=plan, message="计划完成: 1/2 个子任务成功")

    async def resume(self, instruction: str = "") -> AsyncIterator[PlanEvent]:
        self.resumes.append(instruction)
        yield PlanEvent(kind="plan_done", plan=_plan(instruction or "resume"), message="恢复完成")


class _ScriptedTeamExecutor:
    """替身 TeamExecutor：批准后停在 needs_input，用于验证 /team resume 路由。"""

    instances: list["_ScriptedTeamExecutor"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")
        self.project_root = kwargs.get("project_root")
        self.decision: ReviewDecision | None = None
        self.resumed: list[tuple[str, list[ResourceClaim]]] = []
        self.resume_instructions: list[str] = []
        _ScriptedTeamExecutor.instances.append(self)

    async def run(self, goal: str) -> AsyncIterator[TeamEvent]:
        plan = _team_plan(goal)
        yield TeamEvent(kind="team_started", team_id="team-1", message=goal)
        yield TeamEvent(kind="team_plan_generated", team_id="team-1", plan=plan, message="2 个任务")
        yield TeamEvent(kind="team_review", team_id="team-1", plan=plan, message="等待用户审阅团队计划")
        assert self.reviewer is not None
        decision = await self.reviewer(plan)
        self.decision = decision
        if decision.action != "execute":
            yield TeamEvent(kind="cancelled", team_id="team-1", plan=plan, message="用户取消团队计划")
            return
        yield TeamEvent(kind="approved", team_id="team-1", plan=plan, message="团队计划已批准")
        yield TeamEvent(kind="batch_started", team_id="team-1", plan=plan, batch=["t1"], message="第 1 轮 / 共 2 轮")
        plan.tasks[0].status = "needs_input"
        yield TeamEvent(
            kind="task_needs_input",
            team_id="team-1",
            plan=plan,
            task=plan.tasks[0],
            role="coder",
            failure_category="repair_scope_missing",
            message="需要显式写入范围",
        )

    async def resume(self, instruction: str = "") -> AsyncIterator[TeamEvent]:
        self.resume_instructions.append(instruction)
        yield TeamEvent(kind="team_done", team_id="team-1", plan=_team_plan("resume"), message="Team 完成")

    async def resume_task_with_repair_scope(
        self, task_id: str, claims: list[ResourceClaim]
    ) -> AsyncIterator[TeamEvent]:
        self.resumed.append((task_id, list(claims)))
        plan = _team_plan("repair")
        yield TeamEvent(
            kind="task_resume_requested", team_id="team-1", plan=plan, task=plan.tasks[0],
            message=f"恢复 {task_id}",
        )
        yield TeamEvent(kind="team_done", team_id="team-1", plan=plan, message="Team 完成")


@pytest.fixture(autouse=True)
def _patch_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换执行器并清空实例登记，避免测试间互相看到残留。"""
    _ScriptedPlanExecutor.instances = []
    _ScriptedTeamExecutor.instances = []
    monkeypatch.setattr("routivus.agent.plan.PlanExecutor", _ScriptedPlanExecutor)
    monkeypatch.setattr("routivus.agent.team.TeamExecutor", _ScriptedTeamExecutor)


# ==========================================================================
# 测试脚手架
# ==========================================================================


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
        **overrides,  # type: ignore[arg-type]
    )
    return TestClient(
        create_app(
            registry=registry,
            store=store,
            config=config,
            agent_factory=lambda project, session: _FakeAgent(),
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


def _ws(project: dict, session: dict) -> str:
    return f"/api/ws/projects/{project['id']}/sessions/{session['id']}"


def _receive_until(socket: Any, predicate: Any, limit: int = 60) -> list[dict]:
    """收事件直到满足条件或达到次数上限。

    必须给出一个**保证会到达**的终止条件：TestClient 在消息收完后不会自己断开。
    """
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


def _kinds(cards: list[dict]) -> list[str]:
    return [card.get("kind", "") for card in cards]


def _completed(event: dict) -> bool:
    return event.get("type") == "session.status" and event.get("data", {}).get("status") == "completed"


def _error_code(event: dict) -> str:
    """会话通道的错误码在 `data.code` 里（只有终端通道是扁平结构）。"""
    return str(event.get("data", {}).get("code", ""))


def _is_error(code: str) -> Any:
    return lambda item: item.get("type") == "error" and _error_code(item) == code


# ==========================================================================
# /plan
# ==========================================================================


def test_plan_command_requires_goal(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()  # session.snapshot
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan"})
        event = socket.receive_json()

    assert event["type"] == "error"
    assert _error_code(event) == "missing_goal"
    # 参数错误发生在落库之前：不应留下一条用户消息
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_plan_turn_card_shape_and_review_roundtrip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 重构配置层"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")

        review = events[-1]["data"]
        assert review["mode"] == "plan"
        assert review["plan"]["goal"] == "重构配置层"
        assert [task["id"] for task in review["plan"]["tasks"]] == ["t1", "t2"]
        assert review["timeout"] > 0

        generated = [card for card in _cards(events, "plan.updated") if card["kind"] == "plan_generated"]
        assert generated, "计划生成事件未下发"
        # 契约：卡片字段摊平在 data 顶层，不再是 {kind, payload: {...}} 嵌套
        assert "payload" not in generated[0]
        assert generated[0]["plan"]["tasks"][0]["deps"] == []
        assert generated[0]["message"] == "2 个子任务"

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review["review_id"],
            "action": "execute",
        })
        tail = _receive_until(socket, _completed)

    kinds = _kinds(_cards(tail, "plan.updated"))
    assert kinds == ["approved", "batch_started", "subtask_event", "plan_done"]
    resolved = _cards(tail, "plan.review_resolved")
    assert resolved and resolved[0]["action"] == "execute"
    # 子任务内部事件复用 AgentEvent 映射 → 用户能看到实际进展
    deltas = _cards(tail, "message.delta")
    assert any(item["text"] == "正在拆分配置…" for item in deltas)
    # 子任务产出的正文会被落库为 assistant 消息
    completed = _cards(tail, "message.completed")
    assert completed and completed[0]["message"]["content"] == "正在拆分配置…"


def test_plan_review_cancel_skips_execution(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 无关任务"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        review_id = events[-1]["data"]["review_id"]

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review_id,
            "action": "cancel",
        })
        tail = _receive_until(socket, _completed)

    kinds = _kinds(_cards(tail, "plan.updated"))
    assert kinds == ["cancelled"]
    assert "approved" not in kinds and "batch_started" not in kinds


def test_plan_review_replan_carries_feedback(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 拆得太粗"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        review_id = events[-1]["data"]["review_id"]

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review_id,
            "action": "replan",
            "feedback": "先补回归测试再动手",
        })
        tail = _receive_until(socket, _completed)

    resolved = _cards(tail, "plan.review_resolved")
    assert resolved and resolved[0]["action"] == "replan"
    assert resolved[0]["feedback"] == "先补回归测试再动手"
    done = [card for card in _cards(tail, "plan.updated") if card["kind"] == "plan_done"]
    assert done and "先补回归测试再动手" in done[0]["message"]


def test_plan_decision_mismatch_and_invalid_action_are_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 校验幂等性"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        review_id = events[-1]["data"]["review_id"]

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": "rv-deadbeef",
            "action": "execute",
        })
        mismatch = socket.receive_json()
        assert mismatch["type"] == "error" and _error_code(mismatch) == "review_mismatch"

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d2",
            "review_id": review_id,
            "action": "bogus",
        })
        invalid = socket.receive_json()
        assert invalid["type"] == "error" and _error_code(invalid) == "review_not_accepted"

        # 审阅仍然挂起：正确的 review_id + 合法 action 依然生效
        socket.send_json({
            "type": "plan_decision",
            "request_id": "d3",
            "review_id": review_id,
            "action": "execute",
        })
        tail = _receive_until(socket, _completed)

    assert "approved" in _kinds(_cards(tail, "plan.updated"))


def test_plan_decision_without_pending_review_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": "rv-x",
            "action": "execute",
        })
        event = socket.receive_json()

    assert event["type"] == "error" and _error_code(event) == "no_pending_review"


def test_plan_review_timeout_fails_closed(tmp_path: Path) -> None:
    client = _client(tmp_path, approval_timeout=0.3)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 超时用例"})
        tail = _receive_until(socket, _completed)

    resolved = _cards(tail, "plan.review_resolved")
    assert resolved and resolved[0]["action"] == "cancel"
    assert resolved[0]["feedback"] == "review_timeout"
    kinds = _kinds(_cards(tail, "plan.updated"))
    assert "approved" not in kinds and "batch_started" not in kinds


# ==========================================================================
# /team
# ==========================================================================


def test_team_turn_card_shape_and_review(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/team 改造检索层"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")

        review = events[-1]["data"]
        assert review["mode"] == "team"
        assert review["plan"]["goal"] == "改造检索层"

        cards = _cards(events, "team.updated")
        assert _kinds(cards) == ["team_started", "team_plan_generated", "team_review"]
        assert "payload" not in cards[0]
        assert cards[1]["plan"]["tasks"][0]["owner_role"] == "coder"

        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review["review_id"],
            "action": "execute",
        })
        tail = _receive_until(socket, _completed)

    kinds = _kinds(_cards(tail, "team.updated"))
    assert kinds == ["approved", "batch_started", "task_needs_input"]
    needs_input = [card for card in _cards(tail, "team.updated") if card["kind"] == "task_needs_input"]
    assert needs_input[0]["role"] == "coder"
    assert needs_input[0]["failure_category"] == "repair_scope_missing"
    executor = _ScriptedTeamExecutor.instances[0]
    assert executor.project_root is not None


def test_team_resume_routes_write_scope_to_executor(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/team 改造检索层"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        review_id = events[-1]["data"]["review_id"]
        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": review_id,
            "action": "execute",
        })
        _receive_until(socket, _completed)

        # 写入范围必须显式声明，且只作用于声明的模式
        socket.send_json({
            "type": "user_message",
            "request_id": "r2",
            "content": "/team resume t1 --write-scope src/retrieval --write-scope tests/retrieval",
        })
        tail = _receive_until(socket, _completed)

    kinds = _kinds(_cards(tail, "team.updated"))
    assert kinds == ["task_resume_requested", "team_done"]
    executor = _ScriptedTeamExecutor.instances[-1]
    assert len(executor.resumed) == 1
    task_id, claims = executor.resumed[0]
    assert task_id == "t1"
    assert [claim.pattern for claim in claims] == ["src/retrieval", "tests/retrieval"]
    assert {claim.access for claim in claims} == {"write"}


def test_team_resume_without_prior_team_run_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({
            "type": "user_message",
            "request_id": "r1",
            "content": "/team resume t1 --write-scope src",
        })
        events = _receive_until(socket, _is_error("no_resumable_team"))

    assert events and _error_code(events[-1]) == "no_resumable_team"


def test_team_resume_usage_errors_are_reported(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/team resume --write-scope"})
        missing_value = socket.receive_json()
        assert _error_code(missing_value) == "invalid_resume"

        socket.send_json({"type": "user_message", "request_id": "r2", "content": "/team resume t1 t2"})
        two_ids = socket.receive_json()
        assert _error_code(two_ids) == "invalid_resume"

        socket.send_json({"type": "user_message", "request_id": "r3", "content": "/team resume --bogus x"})
        unknown = socket.receive_json()
        assert _error_code(unknown) == "invalid_resume"

        socket.send_json({"type": "user_message", "request_id": "r4", "content": "/team"})
        no_goal = socket.receive_json()
        assert _error_code(no_goal) == "missing_goal"

    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_team_resume_without_scope_uses_resume(tmp_path: Path) -> None:
    """不带 --write-scope 时走 executor.resume()（续跑失败 / 阻塞任务）。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/team 改造检索层"})
        events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
        socket.send_json({
            "type": "plan_decision",
            "request_id": "d1",
            "review_id": events[-1]["data"]["review_id"],
            "action": "execute",
        })
        _receive_until(socket, _completed)

        socket.send_json({"type": "user_message", "request_id": "r2", "content": "/team resume 继续跑剩下的"})
        tail = _receive_until(socket, _completed)

    executor = _ScriptedTeamExecutor.instances[-1]
    assert executor.resume_instructions == ["继续跑剩下的"]
    assert executor.resumed == []
    assert "team_done" in _kinds(_cards(tail, "team.updated"))


# ==========================================================================
# 取消与普通对话不受影响
# ==========================================================================


def test_plain_message_still_goes_to_chat_turn(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "看一下配置文件"})
        tail = _receive_until(socket, _completed)

    # 普通对话不使用计划执行器，也不会产出计划 / 团队卡片
    assert _ScriptedPlanExecutor.instances == []
    assert _ScriptedTeamExecutor.instances == []
    assert _cards(tail, "plan.updated") == []
    assert _cards(tail, "team.updated") == []
    deltas = _cards(tail, "message.delta")
    assert any(item["text"] == "收到：看一下配置文件" for item in deltas)
    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages] == ["user", "assistant"]


def test_cancel_while_awaiting_review_releases_bridge(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 等审阅时取消"})
        _receive_until(socket, lambda item: item.get("type") == "plan.review")

        socket.send_json({"type": "cancel", "request_id": "c1"})
        events = _receive_until(
            socket, lambda item: item.get("type") == "session.status" and item.get("data", {}).get("status") == "cancelled"
        )

    assert events, "取消后未收到 cancelled 状态"
    review = client.app.state.session_reviews[session["id"]]
    assert review.has_pending is False
