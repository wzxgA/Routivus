"""桌面端自然语言续跑的服务端协议级测试（方案 17）。

执行器在这里被换成脚本化替身：真实执行器需要 LLM 与工具注册表，而本文件要验的是
**服务端接线** —— 意图识别入口、单一槽位（`session_resumables`）的登记与清理、
plan / team 两条续跑链路、以及权限类失败的末尾确认卡。真实执行语义由
`tests/test_plan.py`、`tests/test_team.py` 覆盖。

约定：所有 WS 测试都有硬性 receive 上限，回归时失败而不是挂死。
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.plan import Plan, PlanEvent, PlanTask
from routivus.agent.react import AgentEvent
from routivus.agent.team import (
    ResourceClaim,
    TeamEvent,
    TeamPlan,
    TeamTask,
    team_snapshot_data,
)
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


# ==========================================================================
# 替身：脚本化 agent / LLM / 执行器
# ==========================================================================


class _ScriptedLlm:
    """可控的流式 LLM：返回固定 JSON、或直接抛异常（验回退）。"""

    def __init__(self, reply: str = '{"intent": "new_chat"}', error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[str] = []

    async def stream_chat(self, messages: list[Any], tools: list[dict] | None = None) -> AsyncIterator[Any]:
        self.calls.append(str(messages[-1].content) if messages else "")
        if self.error is not None:
            raise self.error
        yield AgentEvent(kind="content", text=self.reply)
        yield AgentEvent(kind="done")


class _FakeAgent:
    """够用的假 agent：计划 / 团队只需要 llm / tools / settings 三个依赖存在。

    `resume_intent_llm=False` 是默认值：让绝大多数用例走**关键词回退**，判定确定；
    需要验 LLM 路径的用例再单独塞 `_ScriptedLlm`。
    """

    def __init__(self, llm: Any = None, **settings: Any) -> None:
        self.llm = llm if llm is not None else object()
        self.tools = object()
        resolved: dict[str, Any] = {"task_max_resumes": 3, "resume_intent_llm": False}
        resolved.update(settings)
        self.settings = SimpleNamespace(**resolved)
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
    """替身 PlanExecutor：`run()` 以失败收尾，`resume()` 记录补充指令。"""

    instances: list["_ScriptedPlanExecutor"] = []
    #: `/plan` 的终态：默认失败（槽位保留）；"完成即清槽"的用例改成 plan_done。
    terminal = "plan_failed"
    #: `resume()` 的终态：默认继续失败（这样才能连续续跑、验配额）。
    resume_terminal = "plan_failed"

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")
        self.resumes: list[str] = []
        _ScriptedPlanExecutor.instances.append(self)

    async def run(self, goal: str) -> AsyncIterator[PlanEvent]:
        plan = _plan(goal)
        yield PlanEvent(kind="plan_generated", plan=plan, message="2 个子任务")
        yield PlanEvent(kind="review", plan=plan, message="等待用户审阅")
        decision = await self.reviewer(plan)
        if decision.action != "execute":
            yield PlanEvent(kind="cancelled", plan=plan, message="用户取消计划")
            return
        yield PlanEvent(kind="approved", plan=plan, message="计划已批准")
        plan.tasks[1].status = "failed"
        if self.terminal == "plan_done":
            yield PlanEvent(kind="plan_done", plan=plan, message="计划完成: 2/2 个子任务成功")
            return
        yield PlanEvent(kind="plan_failed", plan=plan, message="失败子任务数超过上限，终止")

    async def resume(self, instruction: str = "") -> AsyncIterator[PlanEvent]:
        self.resumes.append(instruction)
        plan = _plan("resume")
        yield PlanEvent(
            kind="plan_resume_requested", plan=plan,
            message=f"恢复执行：重跑 2 个子任务；补充指令：{instruction}",
        )
        if self.resume_terminal == "plan_done":
            yield PlanEvent(kind="plan_done", plan=plan, message="恢复完成")
            return
        yield PlanEvent(kind="plan_failed", plan=plan, message="恢复后仍有子任务失败")


class _ScriptedTeamExecutor:
    """替身 TeamExecutor：批准后判任务失败，并按开关写一条真实形状的快照。"""

    instances: list["_ScriptedTeamExecutor"] = []
    #: 失败任务是否标记 `needs_scope`（权限类失败 → 走末尾确认卡）。
    scope_failure = False
    #: 写进快照的续跑计数（验配额时设成上限）。
    initial_resume_count = 0

    def __init__(self, **kwargs: Any) -> None:
        self.reviewer = kwargs.get("reviewer")
        self.plan = kwargs.get("resume_plan")
        self.resume_count = int(kwargs.get("resume_count", 0) or 0)
        self.on_snapshot = kwargs.get("on_snapshot")
        self.resume_instructions: list[str] = []
        self.rescued: list[tuple[str, list[str]]] = []
        _ScriptedTeamExecutor.instances.append(self)

    async def run(self, goal: str) -> AsyncIterator[TeamEvent]:
        plan = _team_plan(goal)
        yield TeamEvent(kind="team_started", team_id="team-1", message=goal)
        yield TeamEvent(kind="team_plan_generated", team_id="team-1", plan=plan, message="2 个任务")
        yield TeamEvent(kind="team_review", team_id="team-1", plan=plan, message="等待用户审阅")
        decision = await self.reviewer(plan)
        if decision.action != "execute":
            yield TeamEvent(kind="cancelled", team_id="team-1", plan=plan, message="用户取消")
            return
        yield TeamEvent(kind="approved", team_id="team-1", plan=plan, message="已批准")
        plan.tasks[0].status = "failed"
        plan.tasks[0].needs_scope = bool(_ScriptedTeamExecutor.scope_failure)
        plan.tasks[0].failure_category = (
            "repair_scope_missing" if plan.tasks[0].needs_scope else "task_failed"
        )
        plan.tasks[0].resource_claims = [ResourceClaim("routivus/web/**", "read")]
        if self.on_snapshot is not None:
            self.on_snapshot(
                team_snapshot_data(
                    plan, team_id="team-1", resume_count=_ScriptedTeamExecutor.initial_resume_count
                )
            )
        yield TeamEvent(
            kind="team_failed", team_id="team-1", plan=plan, task=plan.tasks[0],
            role="coder", failure_category=plan.tasks[0].failure_category,
            message="1 个任务失败", resumable=True,
            resume_count=_ScriptedTeamExecutor.initial_resume_count,
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
    """替换执行器并清空实例 / 开关，避免测试间互相看到残留。"""
    _ScriptedPlanExecutor.instances = []
    _ScriptedPlanExecutor.terminal = "plan_failed"
    _ScriptedPlanExecutor.resume_terminal = "plan_failed"
    _ScriptedTeamExecutor.instances = []
    _ScriptedTeamExecutor.scope_failure = False
    _ScriptedTeamExecutor.initial_resume_count = 0
    monkeypatch.setattr("routivus.agent.plan.PlanExecutor", _ScriptedPlanExecutor)
    monkeypatch.setattr("routivus.agent.team.TeamExecutor", _ScriptedTeamExecutor)


# ==========================================================================
# 测试脚手架
# ==========================================================================


def _client(tmp_path: Path, agent: _FakeAgent | None = None, **overrides: Any) -> TestClient:
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
    agents: list[_FakeAgent] = []

    def factory(project: Any, session: Any) -> _FakeAgent:
        created = agent if agent is not None else _FakeAgent()
        agents.append(created)
        return created

    app = create_app(registry=registry, store=store, config=config, agent_factory=factory)
    app.state.test_agents = agents
    return TestClient(app)


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


def _receive_until(socket: Any, predicate: Any, limit: int = 80) -> list[dict]:
    """收事件直到满足条件或达到次数上限（必须给一个保证会到达的终止条件）。"""
    received: list[dict] = []
    for _ in range(limit):
        try:
            event = socket.receive_json()
        except Exception:  # noqa: BLE001 - 收完事件或连接断开即停
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
    return str(event.get("data", {}).get("code", ""))


def _is_error(code: str) -> Any:
    return lambda item: item.get("type") == "error" and _error_code(item) == code


def _types(events: list[dict]) -> list[tuple[str, Any]]:
    """断言消息里带上一份"到底收到了什么"，失败时不用再猜。"""
    return [
        (str(item.get("type")), (item.get("data") or {}).get("kind") or (item.get("data") or {}).get("code"))
        for item in events
    ]


def _wait_idle(client: TestClient, session_id: str, attempts: int = 200) -> None:
    """等上一轮从 `running_tasks` 里退场，避免下一轮撞上 `session_busy`。"""
    for _ in range(attempts):
        task = client.app.state.running_tasks.get(session_id)
        if task is None or task.done():
            return
        time.sleep(0.01)
    raise AssertionError("上一轮迟迟没有结束")


_ids = itertools.count(1)


def _rid(prefix: str) -> str:
    return f"{prefix}-{next(_ids)}"


def _drive_reviewed_turn(socket: Any, client: TestClient, session: dict, command: str) -> list[dict]:
    """发一条 `/plan` / `/team`，批准审阅并收到轮次收尾。"""
    socket.send_json({"type": "user_message", "request_id": _rid("t"), "content": command})
    events = _receive_until(socket, lambda item: item.get("type") == "plan.review")
    assert events and events[-1]["type"] == "plan.review", f"未收到审阅请求：{events}"
    review_id = events[-1]["data"]["review_id"]
    socket.send_json({
        "type": "plan_decision",
        "request_id": _rid("d"),
        "review_id": review_id,
        "action": "execute",
    })
    tail = _receive_until(socket, _completed)
    _wait_idle(client, session["id"])
    return tail


def _insert_snapshot(
    client: TestClient, project: dict, session: dict, *, resume_count: int = 0, needs_scope: bool = False
) -> None:
    """往 events 表塞一条可恢复快照（服务端读取时取最近一条）。"""
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
        session["id"], project["id"], "team.snapshot",
        team_snapshot_data(plan, team_id="team-9", resume_count=resume_count),
    )


def _slot(client: TestClient, session: dict) -> Any:
    return client.app.state.session_resumables.get(session["id"])


# ==========================================================================
# 核心路径：plan
# ==========================================================================


def test_plan_resume_by_text_reuses_memory_executor(tmp_path: Path) -> None:
    """`/plan` 跑到失败 → 发"继续" → 用**同一个**执行器从断点续跑。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()  # session.snapshot
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        assert _slot(client, session) is not None
        assert _slot(client, session).kind == "plan"

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    kinds = _kinds(_cards(tail, "plan.updated"))
    assert "plan_resume_requested" in kinds
    assert _ScriptedPlanExecutor.instances[-1].resumes == ["继续"]
    # 续跑轮把用户的"继续"留在消息流里
    roles = [item["role"] for item in client.get(f"/api/sessions/{session['id']}/messages").json()]
    assert roles == ["user", "user"]


def test_plan_resume_quota_exhausted_consumes_input(tmp_path: Path) -> None:
    """续跑 3 次后第 4 次：回明确错误，且**消费掉输入**（不能落进普通对话重跑一遍）。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        for _ in range(3):
            socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
            _receive_until(socket, _completed)
            _wait_idle(client, session["id"])

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        events = _receive_until(socket, _is_error("resume_quota_exceeded"))
        _wait_idle(client, session["id"])

    assert _error_code(events[-1]) == "resume_quota_exceeded"
    assert len(_ScriptedPlanExecutor.instances[0].resumes) == 3


def test_plan_done_clears_slot(tmp_path: Path) -> None:
    """计划全部成功 → 槽位清空，之后的"继续"走普通对话（不去续一个做完的任务）。"""
    _ScriptedPlanExecutor.terminal = "plan_done"
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 已经做完的事")
        assert _slot(client, session) is None

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _ScriptedPlanExecutor.instances[0].resumes == []
    assert not _cards(tail, "plan.updated")
    assert any(item["text"] == "收到：继续" for item in _cards(tail, "message.delta"))


def test_cancelled_slot_is_not_resumed(tmp_path: Path) -> None:
    """用户主动取消过的任务不续跑：输入不被消费，走普通对话。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        _slot(client, session).user_cancelled = True

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _ScriptedPlanExecutor.instances[0].resumes == []
    assert any(item["text"] == "收到：继续" for item in _cards(tail, "message.delta"))


# ==========================================================================
# 核心路径：team
# ==========================================================================


def test_team_resume_by_text_reuses_button_pipeline(tmp_path: Path) -> None:
    """`/team` 跑到失败 → 发"继续" → 复用 `run_team_resume_turn`（整轮续跑）。"""
    _ScriptedTeamExecutor.scope_failure = False
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        assert _slot(client, session) is not None and _slot(client, session).kind == "team"

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    kinds = _kinds(_cards(tail, "team.updated"))
    assert kinds == ["team_resume_requested", "team_done"], _types(tail)
    assert _ScriptedTeamExecutor.instances[-1].resume_instructions == [""]
    # 收尾（team_done）后槽位清空，团队卡不再可续
    assert _slot(client, session) is None


def test_latest_task_wins_the_single_slot(tmp_path: Path) -> None:
    """先 /team 再 /plan → "继续"续的是 **plan**（单一槽位、最新覆盖）。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        team_runs = len(_ScriptedTeamExecutor.instances)
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        assert _slot(client, session).kind == "plan"

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert len(_ScriptedTeamExecutor.instances) == team_runs  # 没有多起一轮 team
    assert _ScriptedPlanExecutor.instances[-1].resumes == ["继续"]
    assert "plan_resume_requested" in _kinds(_cards(tail, "plan.updated"))


def test_plan_slot_does_not_resume_team_snapshot(tmp_path: Path) -> None:
    """槽位是 plan 时，库里即使还有 team 快照，文本"继续"也不该续到 team。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _insert_snapshot(client, project, session)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        team_runs = len(_ScriptedTeamExecutor.instances)

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert len(_ScriptedTeamExecutor.instances) == team_runs
    assert _ScriptedPlanExecutor.instances[-1].resumes == ["继续"]
    assert _cards(tail, "team.updated") == []


def test_team_resume_rejected_when_snapshot_has_no_pending_task(tmp_path: Path) -> None:
    """快照里任务全做完 → 槽位清掉、输入不吃，走普通对话。"""
    _ScriptedTeamExecutor.scope_failure = False
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        # 人为把最近一条快照改成"全部完成"
        plan = TeamPlan(
            goal="改造检索层",
            tasks=[TeamTask("t1", "改造检索层", "改造检索层", [], owner_role="coder", status="done")],
            batches=[["t1"]],
        )
        client.app.state.workspace_store.append_event(
            session["id"], project["id"], "team.snapshot",
            team_snapshot_data(plan, team_id="team-1", resume_count=1),
        )
        team_runs = len(_ScriptedTeamExecutor.instances)

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert len(_ScriptedTeamExecutor.instances) == team_runs
    assert _slot(client, session) is None
    assert any(item["text"] == "收到：继续" for item in _cards(tail, "message.delta"))


def test_team_text_resume_over_quota_consumes_input(tmp_path: Path) -> None:
    """team 配额用尽：回明确错误并消费输入，不能让 agent 拿着"继续"重跑。"""
    _ScriptedTeamExecutor.initial_resume_count = 3
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        team_runs = len(_ScriptedTeamExecutor.instances)

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        events = _receive_until(socket, _is_error("resume_quota_exceeded"))
        _wait_idle(client, session["id"])

    assert _error_code(events[-1]) == "resume_quota_exceeded"
    assert len(_ScriptedTeamExecutor.instances) == team_runs


# ==========================================================================
# 不吃输入
# ==========================================================================


def test_other_slash_inputs_never_resume(tmp_path: Path) -> None:
    """`/plan 新任务` 与任何 `/` 开头的输入都不进续跑。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 旧任务")
        first = _ScriptedPlanExecutor.instances[-1]

        # 新的 /plan 覆盖槽位（而不是触发续跑）
        _drive_reviewed_turn(socket, client, session, "/plan 新任务")
        assert first.resumes == []
        assert _slot(client, session).goal == "新任务"

        # 斜杠命令照常走命令通道
        socket.send_json({
            "type": "user_message", "request_id": _rid("m"), "content": "/no-such-command",
        })
        events = _receive_until(socket, lambda item: item.get("type") == "command.executed")
        _wait_idle(client, session["id"])

    assert events, "斜杠命令没有走命令通道"
    assert _ScriptedPlanExecutor.instances[-1].resumes == []


def test_session_busy_wins_over_resume(tmp_path: Path) -> None:
    """会话运行中：续跑也要先停（原有互斥）。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")

        class _Pending:
            def done(self) -> bool:
                return False

        client.app.state.running_tasks[session["id"]] = _Pending()
        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        events = _receive_until(socket, _is_error("session_busy"))

    assert _error_code(events[-1]) == "session_busy"
    assert _ScriptedPlanExecutor.instances[0].resumes == []


# ==========================================================================
# 意图识别与回退
# ==========================================================================


def test_intent_llm_new_chat_does_not_consume_input(tmp_path: Path) -> None:
    """LLM 判为 new_chat → 输入不被消费，走普通对话。"""
    llm = _ScriptedLlm('{"intent": "new_chat"}')
    client = _client(tmp_path, agent=_FakeAgent(llm=llm, resume_intent_llm=True))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")
        assert llm.calls == []  # /plan 自身不触发意图识别

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert llm.calls, "意图识别没有被调用"
    assert _ScriptedPlanExecutor.instances[0].resumes == []
    assert any(item["text"] == "收到：继续" for item in _cards(tail, "message.delta"))


def test_intent_llm_resume_task_runs_even_without_keyword(tmp_path: Path) -> None:
    """LLM 判为 resume_task → 即使没命中关键词也续跑。"""
    llm = _ScriptedLlm('{"intent": "resume_task"}')
    client = _client(tmp_path, agent=_FakeAgent(llm=llm, resume_intent_llm=True))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")

        socket.send_json({
            "type": "user_message", "request_id": _rid("m"), "content": "把刚才那件事办完",
        })
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _ScriptedPlanExecutor.instances[0].resumes == ["把刚才那件事办完"]
    assert "plan_resume_requested" in _kinds(_cards(tail, "plan.updated"))


@pytest.mark.parametrize(
    "reply",
    ["这不是 JSON", '{"intent": "unknown"}', ""],
)
def test_intent_llm_garbage_falls_back_to_keywords(tmp_path: Path, reply: str) -> None:
    """LLM 返回垃圾 → 回退关键词白名单（不 500、不误判）。"""
    llm = _ScriptedLlm(reply)
    client = _client(tmp_path, agent=_FakeAgent(llm=llm, resume_intent_llm=True))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _ScriptedPlanExecutor.instances[0].resumes == ["继续"]


def test_intent_llm_exception_falls_back_to_keywords(tmp_path: Path) -> None:
    """LLM 抛异常 → 回退关键词（"继续"命中，长句不命中）。"""
    llm = _ScriptedLlm(error=RuntimeError("llm down"))
    client = _client(tmp_path, agent=_FakeAgent(llm=llm, resume_intent_llm=True))
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")

        socket.send_json({
            "type": "user_message",
            "request_id": _rid("m"),
            "content": "顺便问一下这个仓库的分支策略是怎么定的",
        })
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])
        assert _ScriptedPlanExecutor.instances[0].resumes == []

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续执行"})
        _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _ScriptedPlanExecutor.instances[0].resumes == ["继续执行"]
    assert any(
        item["text"] == "收到：顺便问一下这个仓库的分支策略是怎么定的"
        for item in _cards(tail, "message.delta")
    )


# ==========================================================================
# 权限类失败：消息流末尾的范围确认卡（§3.8）
# ==========================================================================


def test_needs_scope_pushes_confirmation_card_instead_of_resuming(tmp_path: Path) -> None:
    """`needs_scope` 失败后发"继续" → 不续跑，改为推 `team.scope_requested`。"""
    _ScriptedTeamExecutor.scope_failure = True
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        team_runs = len(_ScriptedTeamExecutor.instances)

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        events = _receive_until(socket, lambda item: item.get("type") == "team.scope_requested")
        _wait_idle(client, session["id"])

    assert events and events[-1]["type"] == "team.scope_requested"
    data = events[-1]["data"]
    assert data["task_id"] == "t1"
    assert data["goal"] == "改造检索层"
    # 候选来自服务端保守提取（该任务已声明的 read claim），**不是**授权
    assert data["candidates"] == ["routivus/web/**"]
    assert len(_ScriptedTeamExecutor.instances) == team_runs  # 没有直接续跑
    assert _cards(events, "team.updated") == []


def test_scope_confirmation_reuses_team_resume_ws_message(tmp_path: Path) -> None:
    """末尾卡确认 → 走现有的 `team_resume` + `scope` → 只救那一个任务。"""
    _ScriptedTeamExecutor.scope_failure = True
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")
        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        _receive_until(socket, lambda item: item.get("type") == "team.scope_requested")
        _wait_idle(client, session["id"])

        socket.send_json({
            "type": "team_resume",
            "request_id": _rid("r"),
            "task_id": "t1",
            "scope": [{"pattern": "routivus/web/**", "access": "write"}],
        })
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    executor = _ScriptedTeamExecutor.instances[-1]
    assert executor.rescued == [("t1", ["routivus/web/**"])]
    assert executor.resume_instructions == []
    assert _kinds(_cards(tail, "team.updated")) == ["team_resume_requested", "team_done"]


def test_scope_confirmation_not_pushed_for_plain_failure(tmp_path: Path) -> None:
    """非 `needs_scope` 的普通失败 → 直接续跑，不弹确认卡。"""
    _ScriptedTeamExecutor.scope_failure = False
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/team 改造检索层")

        socket.send_json({"type": "user_message", "request_id": _rid("m"), "content": "继续"})
        tail = _receive_until(socket, _completed)
        _wait_idle(client, session["id"])

    assert _cards(tail, "team.scope_requested") == []
    assert _ScriptedTeamExecutor.instances[-1].resume_instructions == [""]


# ==========================================================================
# 兼容：按钮链路与清理
# ==========================================================================


def test_button_resume_survives_empty_slot(tmp_path: Path) -> None:
    """团队卡按钮那条链路不经槽位：槽位为空（比如刚重启）时照样能续跑。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _insert_snapshot(client, project, session)
    assert _slot(client, session) is None

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "team_resume", "request_id": _rid("r"), "instruction": "继续"})
        tail = _receive_until(socket, _completed)

    assert _kinds(_cards(tail, "team.updated")) == ["team_resume_requested", "team_done"]
    assert _ScriptedTeamExecutor.instances[-1].resume_instructions == ["继续"]


def test_delete_session_clears_slot(tmp_path: Path) -> None:
    """`delete_session` 后 `session_resumables` 无残留。"""
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        _drive_reviewed_turn(socket, client, session, "/plan 重构配置层")

    assert _slot(client, session) is not None
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    assert client.app.state.session_resumables == {}


def test_resumable_summary_mentions_progress(tmp_path: Path) -> None:
    """槽位摘要只给"够判断"的一行：目标 + 子任务进度。"""
    from routivus.agent.resume import ResumableTask
    from routivus.server.app import _resumable_summary

    class _Executor:
        _last_plan = _plan("重构配置层")

    resume = ResumableTask(kind="plan", goal="重构配置层", plan_executor=_Executor())  # type: ignore[arg-type]
    summary = _resumable_summary(resume)
    assert "重构配置层" in summary
    assert "2 个子任务" in summary

    team = ResumableTask(kind="team", goal="改造检索层")
    assert _resumable_summary(team) == "类型：/team；目标：改造检索层"
