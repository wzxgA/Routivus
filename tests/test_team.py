"""Team MVP 测试。"""

from __future__ import annotations

import json
import re
from io import StringIO
from typing import AsyncIterator

from routivus.agent.plan import ReviewDecision
from routivus.agent.react import AgentEvent
from routivus.agent.team import (
    AgentProfile,
    ReviewResult,
    ResourceClaim,
    ScopedToolRegistry,
    TeamExecutor,
    TeamTask,
    build_repair_scope,
    conflict_safe_batches,
    default_profiles,
    normalize_team_tool_names,
    parse_review_output,
    parse_team_tasks,
    validate_task_resource_policy,
)
from routivus.llm.client import LlmClient
from routivus.llm.types import Message, StreamEvent, ToolCall, ToolResult
from routivus.tool.builtin import build_registry
from routivus.tui.reducer import reduce_team_event

from routivus.tui.state import AgentGroupState, TuiState


TEAM_PLAN = json.dumps({"tasks": [
    {
        "id": "t1", "title": "写入 A", "description": "用 write_file 写 a.txt",
        "deps": [], "owner_role": "coder", "allowed_tools": ["write_file"],
        "resource_claims": [{"pattern": "a.txt", "access": "write"}],
        "acceptance_criteria": ["a.txt 存在"],
    },
    {
        "id": "t2", "title": "写入 B", "description": "用 write_file 写 b.txt",
        "deps": [], "owner_role": "coder", "allowed_tools": ["write_file"],
        "resource_claims": [{"pattern": "b.txt", "access": "write"}],
        "acceptance_criteria": ["b.txt 存在"],
    },
    {
        "id": "t3", "title": "读取验证", "description": "用 read_file 验证两个文件",
        "deps": ["t1", "t2"], "owner_role": "tester", "allowed_tools": ["read_file"],
        "resource_claims": [
            {"pattern": "a.txt", "access": "read"},
            {"pattern": "b.txt", "access": "read"},
        ],
        "acceptance_criteria": ["两个文件内容可读取"],
    },
]}, ensure_ascii=False)


class TeamScriptClient(LlmClient):
    def __init__(self, worker_scripts: dict[str, list], reviews: list[str] | None = None):
        self.worker_scripts = {key: list(value) for key, value in worker_scripts.items()}
        self.reviews = list(reviews or [])
        self.requests: list[list] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        system = messages[0].content if messages else ""
        if "团队任务规划器" in system:
            yield StreamEvent(kind="content", text=TEAM_PLAN)
            yield StreamEvent(kind="done")
            return
        if "严格的任务审查 Agent" in system:
            yield StreamEvent(kind="content", text=self.reviews.pop(0))
            yield StreamEvent(kind="done")
            return

        user = next(message.content for message in messages if message.role == "user")
        match = re.search(r"任务 (t\d+(?:-repair-\d+)?)", user)
        assert match is not None, user
        task_id = match.group(1)
        entry = self.worker_scripts[task_id].pop(0)
        content, calls = entry
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call-{task_id}-{name}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


async def approve_team(plan) -> ReviewDecision:
    return ReviewDecision(action="execute")


async def collect(executor: TeamExecutor, goal: str) -> list:
    return [event async for event in executor.run(goal)]


async def test_team_parallel_workers_artifacts_and_reviews(tmp_path, settings):
    client = TeamScriptClient(
        worker_scripts={
            "t1": [
                ("", [("write_file", json.dumps({"path": "a.txt", "content": "A"}))]),
                ("A 已写入", []),
            ],
            "t2": [
                ("", [("write_file", json.dumps({"path": "b.txt", "content": "B"}))]),
                ("B 已写入", []),
            ],
            "t3": [
                ("", [("read_file", json.dumps({"path": "a.txt"}))]),
                ("验证完成", []),
            ],
        },
        reviews=['{"verdict":"pass","findings":[],"required_fixes":[],"evidence":["ok"]}'] * 3,
    )
    registry = build_registry(base_dir=tmp_path)
    executor = TeamExecutor(
        llm=client, tools=registry, settings=settings,
        reviewer=approve_team, project_root=tmp_path,
    )

    events = await collect(executor, "写入并验证两个文件")

    assert events[-1].kind == "team_done"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
    first_batch = next(event for event in events if event.kind == "batch_started")
    assert first_batch.batch == ["t1", "t2"]
    assert len([event for event in events if event.kind == "artifact_produced"]) >= 5
    reviews = [event for event in events if event.kind == "task_review_done"]
    assert len(reviews) == 3 and all(event.review.verdict == "pass" for event in reviews)
    assert any("你的角色：coder" in message[0].content for message in client.requests)


async def test_team_failed_review_creates_targeted_repair(tmp_path, settings):
    client = TeamScriptClient(
        worker_scripts={
            "t1": [("初始实现", [])],
            "t1-repair-1": [("已修复", [])],
        }
    )
    review_count = 0

    async def review(task: TeamTask, artifacts):
        nonlocal review_count
        review_count += 1
        if review_count == 1:
            return ReviewResult(
                task.id, "fail", ["密码校验错误"], ["修复密码校验"], [],
                [ResourceClaim("src/auth.py", "write")],
            )
        return ReviewResult(task.id, "pass", [], [], ["修复后测试通过"])

    plan = json.dumps({"tasks": [{
        "id": "t1", "title": "登录实现", "description": "实现登录",
        "deps": [], "owner_role": "coder",
        "resource_claims": [{"pattern": "src/auth.py", "access": "write"}],
        "acceptance_criteria": ["密码校验正确"],
    }]}, ensure_ascii=False)
    client.worker_scripts["t1"] = [("初始实现", [])]

    # 替换规划响应为单任务，避免把测试重点绑定到固定示例计划。
    original = client.stream_chat

    async def stream_chat(messages, tools=None):
        if messages and "团队任务规划器" in messages[0].content:
            yield StreamEvent(kind="content", text=plan)
            yield StreamEvent(kind="done")
            return
        async for event in original(messages, tools):
            yield event

    client.stream_chat = stream_chat  # type: ignore[method-assign]
    executor = TeamExecutor(
        llm=client, tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, task_reviewer=review, project_root=tmp_path,
    )
    events = await collect(executor, "实现登录")

    assert events[-1].kind == "team_done"
    assert any(event.kind == "repair_requested" for event in events)
    assert [event.kind for event in events].count("task_review_done") == 2
    assert any(event.role == "repairer" for event in events if event.kind == "agent_started")
    repair_event = next(event for event in events if event.kind == "repair_requested")
    assert repair_event.task.resource_scope_mode == "targeted"
    assert repair_event.task.allowed_tools == []
    assert repair_event.task.resource_claims == [ResourceClaim("src/auth.py", "write")]


def test_repair_scope_never_upgrades_read_claim_to_write():
    original = TeamTask(
        "t1", "调研认证", "读取认证代码", [], owner_role="researcher",
        resource_scope_mode="read_discovery",
        resource_claims=[ResourceClaim("src/auth.py", "read")],
    )
    review = ReviewResult("t1", "fail", ["需要修改"], ["修复认证"], [])

    claims, warnings = build_repair_scope(original, review)

    assert claims == []
    assert "repair_scope" in warnings[0]


def test_resource_policy_rejects_invalid_repairer_scope_and_accepts_targeted_scope(tmp_path):
    profile = default_profiles()["repairer"]
    invalid = TeamTask(
        "t1-repair-1", "修复", "修复问题", [], owner_role="repairer",
        resource_scope_mode="read_discovery",
    )
    errors = validate_task_resource_policy(invalid, profile, tmp_path)
    assert any("不能使用 read_discovery" in error for error in errors)
    assert any(error.startswith("repair_scope_missing") for error in errors)

    valid = TeamTask(
        "t1-repair-1", "修复", "修复问题", [], owner_role="repairer",
        resource_scope_mode="targeted",
        resource_claims=[ResourceClaim("src/auth.py", "write")],
    )
    assert validate_task_resource_policy(valid, profile, tmp_path) == []


def test_team_parser_and_resource_conflict_scheduling():
    tasks, warnings = parse_team_tasks(TEAM_PLAN)
    assert not warnings
    assert [task.owner_role for task in tasks] == ["coder", "coder", "tester"]
    assert conflict_safe_batches(tasks) == [["t1", "t2"], ["t3"]]


def test_review_output_parser_fails_closed_for_empty_and_missing_scope():
    empty = parse_review_output("t1", "   ")
    assert empty.category == "review_output_empty"

    invalid_scope = parse_review_output(
        "t1",
        json.dumps({"verdict": "fail", "repair_scope": [{"pattern": "src/a.py"}]}),
    )
    assert invalid_scope.category == "review_scope_invalid"

    valid = parse_review_output(
        "t1",
        json.dumps({
            "verdict": "fail",
            "findings": ["需要修复"],
            "required_fixes": ["修复实现"],
            "evidence": ["测试失败"],
            "repair_scope": [{"pattern": "src/a.py", "access": "write"}],
        }),
    )
    assert isinstance(valid, ReviewResult)
    assert valid.repair_scope == [ResourceClaim("src/a.py", "write")]


async def test_invalid_reviewer_output_pauses_without_creating_repairer(tmp_path, settings):
    plan = json.dumps({"tasks": [{
        "id": "t1", "title": "实现登录", "description": "实现登录",
        "deps": [], "owner_role": "coder", "acceptance_criteria": ["完成实现"],
    }]}, ensure_ascii=False)
    client = TeamScriptClient(
        worker_scripts={"t1": [("初始实现", [])]},
        reviews=["", ""],
    )
    original = client.stream_chat

    async def stream_chat(messages, tools=None):
        if messages and "团队任务规划器" in messages[0].content:
            yield StreamEvent(kind="content", text=plan)
            yield StreamEvent(kind="done")
            return
        async for event in original(messages, tools):
            yield event

    client.stream_chat = stream_chat  # type: ignore[method-assign]
    executor = TeamExecutor(
        llm=client, tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, project_root=tmp_path,
    )

    events = await collect(executor, "实现登录")

    task = next(event.plan.task_by_id("t1") for event in events if event.kind == "task_needs_input")
    assert task.status == "needs_input"
    assert task.failure_category == "review_output_empty"
    assert not any(event.kind == "repair_requested" for event in events)
    assert not any(event.kind == "task_failed" for event in events)
    assert not any(event.kind == "team_failed" for event in events)
    assert any(event.kind == "review_output_retry" for event in events)
    assert task.repair_attempts_started == 0


async def test_missing_repair_scope_does_not_consume_repair_quota(tmp_path, settings):
    plan = json.dumps({"tasks": [{
        "id": "t1", "title": "调研认证", "description": "读取认证代码",
        "deps": [], "owner_role": "researcher", "resource_scope_mode": "read_discovery",
        "resource_claims": [{"pattern": "src/**", "access": "read"}],
        "acceptance_criteria": ["找出问题"],
    }]}, ensure_ascii=False)
    client = TeamScriptClient(worker_scripts={"t1": [("发现问题", [])]})
    review = ReviewResult("t1", "fail", ["需要修改"], ["修复问题"], [])

    async def task_review(task, artifacts):
        return review

    original = client.stream_chat

    async def stream_chat(messages, tools=None):
        if messages and "团队任务规划器" in messages[0].content:
            yield StreamEvent(kind="content", text=plan)
            yield StreamEvent(kind="done")
            return
        async for event in original(messages, tools):
            yield event

    client.stream_chat = stream_chat  # type: ignore[method-assign]
    executor = TeamExecutor(
        llm=client, tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, task_reviewer=task_review, project_root=tmp_path,
    )

    events = await collect(executor, "调研认证")

    task = next(event.plan.task_by_id("t1") for event in events if event.kind == "repair_scope_required")
    assert task.status == "needs_input"
    assert task.repair_attempts_started == 0
    assert task.repair_attempts_blocked == 0
    assert not any(event.kind == "repair_requested" for event in events)


async def test_user_scope_resume_starts_repairer_and_finishes_task(tmp_path, settings):
    plan = json.dumps({"tasks": [{
        "id": "t1", "title": "实现登录", "description": "实现登录",
        "deps": [], "owner_role": "coder", "acceptance_criteria": ["完成实现"],
    }]}, ensure_ascii=False)
    client = TeamScriptClient(
        worker_scripts={
            "t1": [("初始实现", [])],
            "t1-repair-1": [("修复完成", [])],
        },
    )
    review_count = 0

    async def task_review(task, artifacts):
        nonlocal review_count
        review_count += 1
        if review_count == 1:
            return ReviewResult(task.id, "fail", ["需要修复"], ["修复问题"], [])
        return ReviewResult(task.id, "pass", [], [], ["修复后通过"])

    original = client.stream_chat

    async def stream_chat(messages, tools=None):
        if messages and "团队任务规划器" in messages[0].content:
            yield StreamEvent(kind="content", text=plan)
            yield StreamEvent(kind="done")
            return
        async for event in original(messages, tools):
            yield event

    client.stream_chat = stream_chat  # type: ignore[method-assign]
    executor = TeamExecutor(
        llm=client, tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, task_reviewer=task_review, project_root=tmp_path,
    )

    first_events = await collect(executor, "实现登录")
    assert any(event.kind == "repair_scope_required" for event in first_events)

    resumed = [event async for event in executor.resume_task_with_repair_scope(
        "t1", [ResourceClaim("src/auth.py", "write")]
    )]
    task = next(event.plan.task_by_id("t1") for event in resumed if event.kind == "team_done")
    assert task.status == "done"
    assert any(event.kind == "repair_requested" for event in resumed)
    assert any(event.kind == "agent_started" and event.role == "repairer" for event in resumed)
    assert task.repair_attempts_started == 1

    conflicting = [
        TeamTask("a", "A", "", [], resource_claims=[]),
        TeamTask("b", "B", "", [], resource_claims=[]),
    ]
    assert conflict_safe_batches(conflicting) == [["a"], ["b"]]


def test_profile_tool_intersection_is_explicit():
    profile = AgentProfile("reviewer", "", ("read_file",), is_reviewer=True)
    assert profile.allowed_tools == ("read_file",)


def test_team_resource_scope_supports_read_only_discovery_and_normalized_root(tmp_path):
    profile = default_profiles()["researcher"]
    task = TeamTask(
        "t1", "调研", "调研项目", [], owner_role="researcher",
        resource_scope_mode="read_discovery",
    )
    scoped = ScopedToolRegistry(build_registry(base_dir=tmp_path), task, tmp_path, profile)

    assert scoped._resource_allowed(ToolCall("list", "list_dir", json.dumps({"path": "."})))
    assert scoped._resource_allowed(ToolCall("list-default", "list_dir", "{}"))
    assert scoped._resource_allowed(ToolCall("read", "read_file", json.dumps({"path": "README.md"})))
    assert not scoped._resource_allowed(ToolCall("secret", "read_file", json.dumps({"path": ".env"})))
    assert not scoped._resource_allowed(ToolCall("outside", "list_dir", json.dumps({"path": ".."})))


async def test_read_discovery_filters_sensitive_paths_from_directory_results(tmp_path):
    (tmp_path / "README.md").write_text("ok", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    task = TeamTask(
        "t1", "调研", "调研项目", [], owner_role="researcher",
        resource_scope_mode="read_discovery",
    )
    scoped = ScopedToolRegistry(build_registry(base_dir=tmp_path), task, tmp_path, default_profiles()["researcher"])

    results = await scoped.aexecute_calls([
        ToolCall("list", "list_dir", json.dumps({"path": "."})),
    ])

    assert results[0].ok
    assert "README.md" in results[0].output
    assert ".env" not in results[0].output


async def test_team_stops_after_worker_failure_and_blocks_dependents(tmp_path, settings):
    plan_json = json.dumps({"tasks": [
        {
            "id": "t1", "title": "根任务", "description": "执行根任务", "deps": [],
            "owner_role": "coder", "acceptance_criteria": ["成功"],
        },
        {
            "id": "t2", "title": "依赖任务", "description": "执行依赖任务", "deps": ["t1"],
            "owner_role": "coder", "acceptance_criteria": ["成功"],
        },
    ]}, ensure_ascii=False)

    class PlannerClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            if messages and "团队任务规划器" in messages[0].content:
                yield StreamEvent(kind="content", text=plan_json)
                yield StreamEvent(kind="done")

    class FailingAgent:
        messages: list = []

        async def run(self, prompt):
            yield AgentEvent(kind="error", text="模拟 Worker 失败")

    class Factory:
        def create(self, profile, task):
            return FailingAgent()

    executor = TeamExecutor(
        llm=PlannerClient(),
        tools=build_registry(base_dir=tmp_path),
        settings=settings,
        reviewer=approve_team,
        agent_factory=Factory(),
        project_root=tmp_path,
    )
    events = await collect(executor, "测试失败传播")

    assert [event.kind for event in events].count("batch_started") == 1
    blocked = [event for event in events if event.kind == "task_blocked"]
    assert len(blocked) == 1 and blocked[0].task.id == "t2"
    assert blocked[0].task.status == "blocked"
    assert not any(event.kind == "agent_started" and event.task.id == "t2" for event in events)
    assert "1 个任务失败" in events[-1].message


def test_targeted_scope_does_not_allow_unclaimed_or_omitted_paths(tmp_path):
    task = TeamTask(
        "t1", "实现", "实现功能", [], owner_role="coder",
        resource_claims=[ResourceClaim("src/**", "read")],
    )
    scoped = ScopedToolRegistry(build_registry(base_dir=tmp_path), task, tmp_path, default_profiles()["coder"])

    assert scoped._resource_allowed(ToolCall("read", "read_file", json.dumps({"path": "src/app.py"})))
    assert not scoped._resource_allowed(ToolCall("root", "list_dir", "{}"))
    assert not scoped._resource_allowed(ToolCall("other", "read_file", json.dumps({"path": "README.md"})))


def test_parser_defaults_read_only_research_tasks_to_discovery_mode():
    tasks, warnings = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "调研", "description": "读取项目", "deps": [],
        "owner_role": "researcher", "allowed_tools": ["list_dir"],
        "acceptance_criteria": ["完成调研"],
    }]}))

    assert not warnings
    assert tasks[0].resource_scope_mode == "read_discovery"


def test_parser_normalizes_safe_tool_aliases_for_researcher():
    tasks, warnings = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "调研", "description": "扫描项目", "deps": [],
        "owner_role": "researcher", "allowed_tools": ["find", "glob", "grep"],
        "acceptance_criteria": ["完成调研"],
    }]}))

    assert tasks[0].allowed_tools == ["glob_files", "grep_code"]
    assert tasks[0].invalid_tools == []
    assert any("find 已转换为 glob_files" in warning for warning in warnings)
    assert any("glob 已转换为 glob_files" in warning for warning in warnings)
    assert any("grep 已转换为 grep_code" in warning for warning in warnings)


def test_unknown_explicit_tools_fail_closed_instead_of_exposing_all_tools():
    tasks, warnings = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "调研", "description": "扫描项目", "deps": [],
        "owner_role": "researcher", "allowed_tools": ["scan_project"],
        "acceptance_criteria": ["完成调研"],
    }]}))

    assert tasks[0].allowed_tools == []
    assert tasks[0].allowed_tools_declared is True
    assert tasks[0].invalid_tools == ["scan_project"]
    assert any("scan_project 不是已注册工具" in warning for warning in warnings)
    assert any("计划包含无效工具" in error for error in validate_task_resource_policy(
        tasks[0], default_profiles()["researcher"]
    ))


async def test_explicit_empty_tools_do_not_fallback_to_profile_tools(tmp_path):
    task = TeamTask(
        "t1", "空工具", "不调用工具", [], owner_role="researcher",
        allowed_tools=[], allowed_tools_declared=True,
        resource_scope_mode="read_discovery",
    )
    scoped = ScopedToolRegistry(build_registry(base_dir=tmp_path), task, tmp_path, default_profiles()["researcher"])

    assert scoped.schemas() == []
    results = await scoped.aexecute_calls([
        ToolCall("call-1", "list_dir", json.dumps({"path": "."})),
    ])
    assert not results[0].ok
    assert "未允许任何工具调用" in results[0].error


def test_failed_task_marks_downstream_as_blocked_not_failed():
    from routivus.agent.team import TeamPlan

    t1 = TeamTask("t1", "根任务", "", [], status="failed")
    t2 = TeamTask("t2", "直接依赖", "", ["t1"])
    t3 = TeamTask("t3", "间接依赖", "", ["t2"])
    t4 = TeamTask("t4", "独立任务", "", [])
    plan = TeamPlan("测试", [t1, t2, t3, t4], [["t1"], ["t2"], ["t3", "t4"]])

    blocked = TeamExecutor._block_dependents(plan, {"t1"})

    assert [task.id for task in blocked] == ["t2", "t3"]
    assert t2.status == "blocked" and t2.blocked_by == ["t1"]
    assert t3.status == "blocked" and t3.blocked_by == ["t2"]
    assert t4.status == "pending"


def test_team_events_keep_role_and_task_progress_in_tui_state():
    tasks, _ = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "实现", "description": "实现代码", "deps": [],
        "owner_role": "coder", "acceptance_criteria": ["完成"],
    }]}))
    from routivus.agent.team import TeamPlan, TeamEvent

    plan = TeamPlan("demo", tasks, [["t1"]])
    state = TuiState(active_turn_id="turn-1")
    state = reduce_team_event(state, TeamEvent("team_plan_generated", team_id="team-1", plan=plan), "turn-1")
    assert state.phase == "awaiting_plan_review"
    state = reduce_team_event(state, TeamEvent("task_started", team_id="team-1", plan=plan, task=tasks[0], role="coder"), "turn-1")
    assert state.plan_tasks["t1"] == "running"
    assert any("coder/t1" in item.text for item in state.transcript)


def test_team_needs_input_is_visible_without_becoming_failure():
    from routivus.agent.team import TeamEvent, TeamPlan

    tasks, _ = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "修复认证", "description": "修复问题", "deps": [],
        "owner_role": "coder", "acceptance_criteria": ["通过验证"],
    }]}))
    plan = TeamPlan("修复认证", tasks, [["t1"]])
    state = TuiState(active_turn_id="turn-1")
    state = reduce_team_event(
        state,
        TeamEvent("task_needs_input", team_id="team-1", plan=plan, task=tasks[0],
                  failure_category="repair_scope_missing", message="请确认写入范围"),
        "turn-1",
    )

    assert state.phase == "awaiting_team_input"
    assert state.plan_tasks["t1"] == "needs_input"
    assert state.inspector.plan.failure_count == 0
    assert state.team_input_task_id == "t1"
    assert state.notification == "请确认写入范围"


def test_team_agent_events_are_isolated_in_default_collapsed_groups():
    from routivus.agent.team import TeamEvent, TeamPlan

    tasks, _ = parse_team_tasks(json.dumps({"tasks": [
        {"id": "t1", "title": "实现 A", "description": "实现 A", "deps": []},
        {"id": "t2", "title": "实现 B", "description": "实现 B", "deps": []},
    ]}))
    plan = TeamPlan("demo", tasks, [["t1", "t2"]])
    state = TuiState(active_turn_id="turn-1")

    state = reduce_team_event(
        state, TeamEvent("team_plan_generated", team_id="team-1", plan=plan), "turn-1"
    )
    for task, agent_id in zip(tasks, ("agent-a", "agent-b")):
        state = reduce_team_event(
            state,
            TeamEvent("agent_started", team_id="team-1", plan=plan, task=task,
                      agent_id=agent_id, role="coder"),
            "turn-1",
        )
        state = reduce_team_event(
            state,
            TeamEvent(
                "subtask_event", team_id="team-1", plan=plan, task=task,
                agent_id=agent_id, role="coder",
                agent_event=AgentEvent(kind="thinking", text=f"检查 {task.id}"),
            ),
            "turn-1",
        )

    assert state.agent_group_order == ["team-1:agent-a", "team-1:agent-b"]
    assert all(group.collapsed for group in state.agent_groups.values())
    assert state.agent_groups["team-1:agent-a"].entries[0].text == "检查 t1"
    assert state.agent_groups["team-1:agent-b"].entries[0].text == "检查 t2"
    assert [item.kind for item in state.transcript].count("agent_group") == 2


def test_team_repair_and_review_have_separate_group_identities():
    from routivus.agent.team import TeamEvent, TeamPlan

    tasks, _ = parse_team_tasks(json.dumps({"tasks": [{
        "id": "t1", "title": "实现登录", "description": "实现登录", "deps": [],
    }]}))
    plan = TeamPlan("demo", tasks, [["t1"]])
    repair = TeamTask("t1-repair-1", "修复登录", "修复问题", [], owner_role="repairer")
    state = TuiState(active_turn_id="turn-1")

    state = reduce_team_event(
        state,
        TeamEvent("task_review_started", team_id="team-1", plan=plan, task=tasks[0], role="reviewer"),
        "turn-1",
    )
    state = reduce_team_event(
        state,
        TeamEvent(
            "task_review_done", team_id="team-1", plan=plan, task=tasks[0], role="reviewer",
            review=ReviewResult("t1", "fail", ["需要修复"], ["修复"], []),
        ),
        "turn-1",
    )
    state = reduce_team_event(
        state,
        TeamEvent("repair_requested", team_id="team-1", plan=plan, task=repair, role="repairer", message="修复"),
        "turn-1",
    )
    state = reduce_team_event(
        state,
        TeamEvent("agent_started", team_id="team-1", plan=plan, task=repair,
                  agent_id="agent-repair", role="repairer"),
        "turn-1",
    )
    state = reduce_team_event(
        state,
        TeamEvent(
            "subtask_event", team_id="team-1", plan=plan, task=repair,
            agent_id="agent-repair", role="repairer",
            agent_event=AgentEvent(kind="tool_result", tool_result=ToolResult(
                tool_call_id="call-1", name="read_file", ok=True, output="ok"
            )),
        ),
        "turn-1",
    )

    assert "team-1:reviewer:t1" in state.agent_groups
    assert "team-1:agent-repair" in state.agent_groups
    assert state.agent_groups["team-1:agent-repair"].task_id == "t1-repair-1"
    assert state.agent_groups["team-1:reviewer:t1"].status == "failed"
    assert any(item.kind == "agent_group" for item in state.transcript)


async def test_readonly_step_limit_recovers_once_with_preserved_artifacts(tmp_path, settings):
    plan_json = json.dumps({"tasks": [{
        "id": "t1", "title": "调研项目", "description": "读取项目结构",
        "deps": [], "owner_role": "researcher", "allowed_tools": ["list_dir"],
        "resource_scope_mode": "read_discovery",
        "acceptance_criteria": ["输出项目结构"],
    }]}, ensure_ascii=False)

    class PlannerClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            if messages and "团队任务规划器" in messages[0].content:
                yield StreamEvent(kind="content", text=plan_json)
                yield StreamEvent(kind="done")

    class StepAgent:
        def __init__(self, recovery: bool):
            self.recovery = recovery
            self.messages = [Message(role="assistant", content="恢复后的调研结果" if recovery else "部分结果")]

        async def run(self, prompt):
            if not self.recovery:
                yield AgentEvent(kind="tool_result", tool_result=ToolResult(
                    tool_call_id="call-t1-list", name="list_dir", ok=True, output="[dir] src"
                ))
                yield AgentEvent(kind="step_limit")
                return
            yield AgentEvent(kind="content", text="补齐验收项")
            yield AgentEvent(kind="done")

    class Factory:
        def __init__(self):
            self.calls = 0

        def create(self, profile, task):
            self.calls += 1
            return StepAgent(recovery=self.calls == 2)

    async def review(task: TeamTask, artifacts):
        return ReviewResult(task.id, "pass", [], [], ["已有证据"])

    settings.team_recovery_steps = 3
    settings.team_max_recoveries = 1
    factory = Factory()
    executor = TeamExecutor(
        llm=PlannerClient(), tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, task_reviewer=review, agent_factory=factory,
        project_root=tmp_path,
    )

    events = await collect(executor, "调研项目")

    task = next(event.plan.task_by_id("t1") for event in events if event.kind == "team_done")
    retry = next(event for event in events if event.kind == "task_retry_started")
    starts = [event for event in events if event.kind == "agent_started"]
    assert task.status == "done"
    assert factory.calls == 2
    assert [event.attempt for event in starts] == [1, 2]
    assert [event.effective_steps for event in starts] == [20, 3]
    assert retry.preserved_artifacts
    assert len(task.artifacts) >= 1
    assert not any(event.kind == "task_failed" for event in events)


async def test_writable_step_limit_is_not_automatically_retried(tmp_path, settings):
    plan_json = json.dumps({"tasks": [{
        "id": "t1", "title": "实现功能", "description": "修改项目",
        "deps": [], "owner_role": "coder", "allowed_tools": ["write_file"],
        "resource_claims": [{"pattern": "src/**", "access": "write"}],
        "acceptance_criteria": ["完成修改"],
    }]}, ensure_ascii=False)

    class PlannerClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            if messages and "团队任务规划器" in messages[0].content:
                yield StreamEvent(kind="content", text=plan_json)
                yield StreamEvent(kind="done")

    class FailingAgent:
        messages = []

        async def run(self, prompt):
            yield AgentEvent(kind="step_limit")

    class Factory:
        calls = 0

        def create(self, profile, task):
            self.calls += 1
            return FailingAgent()

    factory = Factory()
    executor = TeamExecutor(
        llm=PlannerClient(), tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, agent_factory=factory, project_root=tmp_path,
    )

    events = await collect(executor, "实现功能")

    failed = next(event for event in events if event.kind == "task_failed")
    assert factory.calls == 1
    assert failed.failure_category == "step_limit"
    assert not any(event.kind == "task_retry_started" for event in events)


async def collect_team_resume(executor: TeamExecutor, instruction: str = "") -> list:
    return [event async for event in executor.resume(instruction)]


async def test_team_resume_without_plan_fails(tmp_path, settings):
    """尚未执行/生成计划就 resume → team_failed。"""
    client = TeamScriptClient(worker_scripts={})
    executor = TeamExecutor(
        llm=client, tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, project_root=tmp_path,
    )
    events = await collect_team_resume(executor)
    assert [event.kind for event in events] == ["team_failed"]
    assert "没有可恢复" in events[0].message


async def test_team_resume_reruns_failed_and_blocked_skips_done(tmp_path, settings):
    """断点续跑：重跑失败的 t1、重跑被阻塞的 t2、跳过已完成的独立任务 t3。"""
    settings.plan_max_failures = 0
    plan_json = json.dumps({"tasks": [
        {
            "id": "t1", "title": "根任务", "description": "执行根任务", "deps": [],
            "owner_role": "coder", "acceptance_criteria": ["成功"],
        },
        {
            "id": "t2", "title": "依赖任务", "description": "执行依赖任务", "deps": ["t1"],
            "owner_role": "coder", "acceptance_criteria": ["成功"],
        },
        {
            "id": "t3", "title": "独立任务", "description": "执行独立任务", "deps": [],
            "owner_role": "researcher", "allowed_tools": ["read_file"],
            "resource_scope_mode": "read_discovery",
            "resource_claims": [{"pattern": "README.md", "access": "read"}],
            "acceptance_criteria": ["成功"],
        },
    ]}, ensure_ascii=False)

    class PlannerClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            if messages and "团队任务规划器" in messages[0].content:
                yield StreamEvent(kind="content", text=plan_json)
                yield StreamEvent(kind="done")

    class FailingAgent:
        messages: list = []

        async def run(self, prompt):
            yield AgentEvent(kind="error", text="模拟 Worker 失败")

    class SuccessAgent:
        messages: list

        def __init__(self):
            self.messages = [Message(role="assistant", content="执行成功")]

        async def run(self, prompt):
            yield AgentEvent(kind="done")

    class Factory:
        def __init__(self):
            self.calls = {"t1": 0, "t2": 0, "t3": 0}

        def create(self, profile, task):
            self.calls[task.id] = self.calls.get(task.id, 0) + 1
            # 首轮仅 t1 失败 → 阻塞 t2；t2 初次运行也让它失败以验证续跑跳过 done
            if task.id == "t1" and self.calls["t1"] == 1:
                return FailingAgent()
            return SuccessAgent()

    factory = Factory()

    async def review_pass(task, artifacts):
        return ReviewResult(task.id, "pass", [], [], ["测试通过"])

    executor = TeamExecutor(
        llm=PlannerClient(), tools=build_registry(base_dir=tmp_path), settings=settings,
        reviewer=approve_team, task_reviewer=review_pass, agent_factory=factory,
        project_root=tmp_path,
    )

    first = await collect(executor, "测试失败传播")
    assert any(event.kind == "task_failed" and event.task.id == "t1" for event in first)
    blocked = next(event for event in first if event.kind == "task_blocked" and event.task.id == "t2")
    assert blocked.task.status == "blocked"
    # t3 与 t1 同批并行，t1 失败前 t3 已完成
    done_t3 = next(event for event in first if event.kind == "task_done" and event.task.id == "t3")
    assert done_t3.task.status == "done"
    assert first[-1].kind == "team_failed"

    resumed = await collect_team_resume(executor, "补充指令")
    assert any(event.kind == "team_resume_requested" for event in resumed)
    started = [event.task.id for event in resumed if event.kind == "task_started"]
    # t1 重跑、t2 解除阻塞重跑；t3 已 done 跳过
    assert "t1" in started and "t2" in started and "t3" not in started
    assert resumed[-1].kind == "team_done"
    assert all(task.status == "done" for task in executor._last_plan.tasks)
