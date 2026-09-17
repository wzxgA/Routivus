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
    """替身 TeamExecutor：批准后直接判任务失败（不再有 needs_input 等待）。

    续跑相关（方案 15）：`resume()` / `resume_task_with_repair_scope()` 记录调用参数，
    并回放一条恢复事件 + 终态，用来验证服务端的接线（真正的语义在 test_team.py）。
    """

    instances: list["_ScriptedTeamExecutor"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")
        self.project_root = kwargs.get("project_root")
        self.plan = kwargs.get("resume_plan")
        self.resume_count = int(kwargs.get("resume_count", 0) or 0)
        self.on_snapshot = kwargs.get("on_snapshot")
        self.decision: ReviewDecision | None = None
        self.resume_instructions: list[str] = []
        self.rescued: list[tuple[str, list[str]]] = []
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
        plan.tasks[0].status = "failed"
        yield TeamEvent(
            kind="task_failed",
            team_id="team-1",
            plan=plan,
            task=plan.tasks[0],
            role="coder",
            failure_category="repair_scope_missing",
            message="无法安全确定修改范围",
        )

    async def resume(self, instruction: str = "") -> AsyncIterator[TeamEvent]:
        self.resume_instructions.append(instruction)
        plan = self.plan or _team_plan("resume")
        yield TeamEvent(
            kind="team_resume_requested", team_id="team-1", plan=plan,
            message="从断点继续", resume_count=self.resume_count + 1,
        )
        yield TeamEvent(kind="team_done", team_id="team-1", plan=plan, message="Team 完成")

    async def resume_task_with_repair_scope(
        self, task_id: str, claims: list[Any]
    ) -> AsyncIterator[TeamEvent]:
        self.rescued.append((task_id, [claim.pattern for claim in claims]))
        plan = self.plan or _team_plan("resume")
        yield TeamEvent(
            kind="team_resume_requested", team_id="team-1", plan=plan, task=plan.tasks[0],
            role="repairer", message=f"已确认修复范围：{task_id}",
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
    # 子任务产出的正文按段落落库为 assistant 消息，由 message.segment 下发（方案 07 §4.1）
    segments = _cards(tail, "message.segment")
    assert segments and segments[0]["message"]["content"] == "正在拆分配置…"
    assert segments[0]["message"]["role"] == "assistant"
    assert segments[0]["source"] == "task:t1"


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
    assert kinds == ["approved", "batch_started", "task_failed"]
    failed = [card for card in _cards(tail, "team.updated") if card["kind"] == "task_failed"]
    assert failed[0]["role"] == "coder"
    assert failed[0]["failure_category"] == "repair_scope_missing"
    executor = _ScriptedTeamExecutor.instances[0]
    assert executor.project_root is not None


def test_team_resume_is_rejected_as_unknown_command(tmp_path: Path) -> None:
    """`/team resume` 已移除：明确报未知命令，不能落进 /team 被当成新任务重跑。"""
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
        event = socket.receive_json()

    assert _error_code(event) == "unknown_command"
    # 没有真的起一轮 Team：不返回任何 team.updated 卡片
    assert event["type"] == "error"


def test_team_requires_goal_and_rejects_bare_resume(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/team resume"})
        bare_resume = socket.receive_json()
        assert _error_code(bare_resume) == "unknown_command"

        socket.send_json({"type": "user_message", "request_id": "r2", "content": "/team"})
        no_goal = socket.receive_json()
        assert _error_code(no_goal) == "missing_goal"

    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


# ==========================================================================
# Team 断点续跑（方案 15）：快照 → 团队卡「继续」→ 执行器
# ==========================================================================


class _PendingTask:
    """占位"正在跑的轮次"：服务端的互斥判断只看 `done()`。"""

    def done(self) -> bool:
        return False


def _insert_snapshot(
    client: TestClient,
    project: dict,
    session: dict,
    *,
    resume_count: int = 0,
    needs_scope: bool = False,
) -> None:
    """往 events 表塞一条可恢复快照（服务端读取时取最近一条）。"""
    from routivus.agent.team import TeamPlan, TeamTask, team_snapshot_data

    plan = TeamPlan(
        goal="改造检索层",
        tasks=[
            TeamTask(
                "t1", "改造检索层", "改造检索层", [], owner_role="coder",
                status="failed", failure_category="repair_scope_missing",
                needs_scope=needs_scope, result="无法安全确定修改范围",
                resource_claims=[ResourceClaim("routivus/web/**", "read")],
            ),
            TeamTask("t2", "补测试", "补测试", ["t1"], owner_role="tester", status="pending"),
        ],
        batches=[["t1"], ["t2"]],
    )
    client.app.state.workspace_store.append_event(
        session["id"],
        project["id"],
        "team.snapshot",
        team_snapshot_data(plan, team_id="team-9", resume_count=resume_count),
    )


def test_team_resume_without_snapshot_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r1"})
        events = _receive_until(socket, _is_error("no_resumable_team"))

    assert _error_code(events[-1]) == "no_resumable_team"


def test_team_resume_rebuilds_plan_from_snapshot(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _insert_snapshot(client, project, session)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r1", "instruction": "继续"})
        tail = _receive_until(socket, _completed)

    assert _kinds(_cards(tail, "team.updated")) == ["team_resume_requested", "team_done"]
    executor = _ScriptedTeamExecutor.instances[-1]
    # 任务图来自快照（**没有重新规划**）：goal 与任务状态都对得上
    assert executor.plan is not None
    assert executor.plan.goal == "改造检索层"
    assert [task.id for task in executor.plan.tasks] == ["t1", "t2"]
    assert executor.plan.task_by_id("t1").status == "failed"
    assert executor.resume_count == 0
    assert executor.resume_instructions == ["继续"]


def test_team_resume_with_scope_rescues_task(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _insert_snapshot(client, project, session, needs_scope=True)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({
            "type": "team_resume",
            "request_id": "r1",
            # 不指定 task_id：服务端挑第一个"补个范围就能救"的任务
            "scope": [{"pattern": "routivus/web/**", "access": "write"}],
        })
        tail = _receive_until(socket, _completed)

    assert _kinds(_cards(tail, "team.updated")) == ["team_resume_requested", "team_done"]
    executor = _ScriptedTeamExecutor.instances[-1]
    assert executor.rescued == [("t1", ["routivus/web/**"])]
    assert executor.resume_instructions == []


def test_team_resume_rejects_malformed_scope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _insert_snapshot(client, project, session)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r1", "scope": "not-a-list"})
        malformed = _receive_until(socket, _is_error("invalid_scope"))

    assert _error_code(malformed[-1]) == "invalid_scope"


def test_team_resume_rejects_over_quota(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    # 配额（task_max_resumes 默认 3）已用完的快照：连快照重建都不做，直接拒
    _insert_snapshot(client, project, session, resume_count=3)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r1"})
        quota = _receive_until(socket, _is_error("resume_quota_exceeded"))

    assert _error_code(quota[-1]) == "resume_quota_exceeded"


def test_team_resume_rejects_duplicate_and_busy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    # 无快照：第一轮会以 no_resumable_team 收尾，正好用来让第一个 request_id 被消费
    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r1"})
        _receive_until(socket, _is_error("no_resumable_team"))
        socket.send_json({"type": "team_resume", "request_id": "r1"})
        duplicate = _receive_until(socket, _is_error("duplicate_request"))

    assert _error_code(duplicate[-1]) == "duplicate_request"

    # 会话正在跑：续跑也要先停
    client.app.state.running_tasks[session["id"]] = _PendingTask()
    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": "r2"})
        busy = _receive_until(socket, _is_error("session_busy"))

    assert _error_code(busy[-1]) == "session_busy"


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
