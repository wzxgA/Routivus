"""计划模式单元测试：拆解解析 / DAG 批次 / 审阅 / 执行器。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import AsyncIterator

import pytest

from routivus.agent.plan import (
    PlanError,
    PlanEvent,
    PlanExecutor,
    PlanTask,
    ReviewDecision,
    build_batches,
    parse_tasks,
)
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient, LlmError
from routivus.llm.types import Message, StreamEvent, ToolCall, ToolResult
from routivus.safety.guards import guard_tool_call
from routivus.safety.hitl import HITLPolicy
from routivus.tool.builtin import build_registry
from routivus.tool.registry import Tool, ToolRegistry


def _task(tid: str, deps: list[str] | None = None) -> PlanTask:
    return PlanTask(id=tid, title=f"标题{tid}", description=f"说明{tid}", deps=deps or [])


def _plan_json(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


VALID_PLAN = _plan_json([
    {"id": "t1", "title": "创建文件", "description": "用 write_file 写入", "deps": []},
    {"id": "t2", "title": "运行测试", "description": "用 execute_command 运行 pytest", "deps": ["t1"]},
])


# ---------- 拆解输出解析 ----------


class TestParseTasks:
    def test_valid_json(self):
        tasks, warnings = parse_tasks(VALID_PLAN)
        assert [t.id for t in tasks] == ["t1", "t2"]
        assert tasks[1].deps == ["t1"]
        assert warnings == []

    def test_markdown_fenced(self):
        raw = f"好的，这是计划：\n```json\n{VALID_PLAN}\n```\n"
        tasks, _ = parse_tasks(raw)
        assert len(tasks) == 2

    def test_json_with_surrounding_text(self):
        tasks, _ = parse_tasks(f"计划如下 {VALID_PLAN} 请确认")
        assert len(tasks) == 2

    def test_missing_description_falls_back_to_title(self):
        tasks, _ = parse_tasks(_plan_json([{"id": "t1", "title": "只有标题"}]))
        assert tasks[0].description == "只有标题"

    def test_invalid_json_raises(self):
        with pytest.raises(PlanError, match="JSON 解析失败"):
            parse_tasks("{broken")

    def test_missing_top_level_raises(self):
        with pytest.raises(PlanError, match="顶层结构"):
            parse_tasks('{"subtasks": []}')

    def test_empty_tasks_raises(self):
        with pytest.raises(PlanError, match="至少需要一个子任务"):
            parse_tasks('{"tasks": []}')

    def test_missing_id_or_title_raises(self):
        with pytest.raises(PlanError, match="缺少 id 或 title"):
            parse_tasks(_plan_json([{"id": "t1"}, {"title": "x", "deps": []}]))

    def test_duplicate_ids_raises(self):
        raw = _plan_json([
            {"id": "t1", "title": "a", "deps": []},
            {"id": "t1", "title": "b", "deps": []},
        ])
        with pytest.raises(PlanError, match="重复"):
            parse_tasks(raw)

    def test_unknown_dep_removed_with_warning(self):
        raw = _plan_json([
            {"id": "t1", "title": "a", "deps": []},
            {"id": "t2", "title": "b", "deps": ["t9"]},
        ])
        tasks, warnings = parse_tasks(raw)
        assert tasks[1].deps == []
        assert any("t9" in w for w in warnings)

    def test_self_dep_removed_with_warning(self):
        raw = _plan_json([{"id": "t1", "title": "a", "deps": ["t1"]}])
        tasks, warnings = parse_tasks(raw)
        assert tasks[0].deps == []
        assert any("自依赖" in w for w in warnings)

    def test_cycle_raises(self):
        raw = _plan_json([
            {"id": "t1", "title": "a", "deps": ["t2"]},
            {"id": "t2", "title": "b", "deps": ["t1"]},
        ])
        with pytest.raises(PlanError, match="环"):
            parse_tasks(raw)

    def test_truncate_over_limit(self):
        tasks_raw = [
            {"id": f"t{i}", "title": f"任务{i}", "deps": [f"t{i-1}"] if i else []}
            for i in range(15)
        ]
        tasks, warnings = parse_tasks(_plan_json(tasks_raw), max_subtasks=12)
        assert len(tasks) == 12
        assert any("截断" in w for w in warnings)
        # 被截断任务的依赖也应被清理，保证可执行
        build_batches(tasks)

    def test_dep_dedup(self):
        raw = _plan_json([
            {"id": "t1", "title": "a", "deps": []},
            {"id": "t2", "title": "b", "deps": ["t1", "t1"]},
        ])
        tasks, _ = parse_tasks(raw)
        assert tasks[1].deps == ["t1"]


# ---------- DAG → 批次（Kahn） ----------


class TestBuildBatches:
    def test_linear_chain(self):
        batches = build_batches([_task("t1"), _task("t2", ["t1"]), _task("t3", ["t2"])])
        assert batches == [["t1"], ["t2"], ["t3"]]

    def test_diamond(self):
        batches = build_batches([
            _task("t1"),
            _task("t2", ["t1"]),
            _task("t3", ["t1"]),
            _task("t4", ["t2", "t3"]),
        ])
        assert batches == [["t1"], ["t2", "t3"], ["t4"]]

    def test_multi_root(self):
        batches = build_batches([_task("t2"), _task("t1"), _task("t3", ["t1"])])
        assert batches == [["t1", "t2"], ["t3"]]

    def test_spec_example(self):
        """设计文档 8.1 示例：T1 → {T2, T3} → {T4, T5}。"""
        batches = build_batches([
            _task("t1"),
            _task("t2", ["t1"]),
            _task("t3", ["t1"]),
            _task("t4", ["t2"]),
            _task("t5", ["t2", "t3"]),
        ])
        assert batches == [["t1"], ["t2", "t3"], ["t4", "t5"]]

    def test_cycle_raises(self):
        with pytest.raises(PlanError):
            build_batches([_task("t1", ["t2"]), _task("t2", ["t1"])])

    def test_unknown_dep_raises(self):
        """deps 引用不存在的 id：无法满足，按环处理。"""
        with pytest.raises(PlanError):
            build_batches([_task("t1", ["tX"])])


# ---------- 执行器（mock LLM） ----------


class PlanScriptedClient(LlmClient):
    """计划模式 mock。

    - 规划调用（system 含「任务规划器」）：依次弹出 plan_script 的原始输出
    - 子任务调用：按 user 消息中的「子任务 tN」路由脚本，脚本项为 (content, calls) 或 LlmError
    """

    def __init__(self, plan_script: list[str], subtask_script: dict[str, list]):
        self.plan_script = list(plan_script)
        self.subtask_script = {k: list(v) for k, v in subtask_script.items()}
        self.requests: list[list[Message]] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        system = messages[0].content if messages else ""
        if "任务规划器" in system:
            raw = self.plan_script.pop(0)
            yield StreamEvent(kind="content", text=raw)
            yield StreamEvent(kind="done", finish_reason="stop")
            return

        user = next(m.content for m in messages if m.role == "user")
        m = re.search(r"子任务 (t\d+)", user)
        assert m is not None, f"无法从 user 消息识别子任务 id: {user[:80]}"
        tid = m.group(1)
        entry = self.subtask_script[tid].pop(0)
        if isinstance(entry, LlmError):
            raise entry
        content, calls = entry
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call_{tid}_{name}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


class SpyAudit:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.entries: list[dict] = []

    def record(self, action: str, **fields) -> None:
        self.actions.append(action)
        self.entries.append({"action": action, **fields})

    def tool_call(self, tool, args, ok, duration_ms, approved=True) -> None:
        self.record("tool_call", tool=tool, ok=ok)

    def approval(self, tool, args, decision, reason="") -> None:
        self.record("approval", tool=tool, decision=decision)

    def blocked(self, reason, **detail) -> None:
        self.record("blocked", reason=reason)


async def review_execute(plan):
    return ReviewDecision(action="execute")


async def review_cancel(plan):
    return ReviewDecision(action="cancel")


def make_executor(
    llm: LlmClient,
    settings: Settings,
    registry: ToolRegistry | None = None,
    reviewer=None,
    approval_policy: HITLPolicy | None = None,
    audit=None,
) -> PlanExecutor:
    return PlanExecutor(
        llm=llm,
        tools=registry or build_registry(),
        settings=settings,
        reviewer=reviewer,
        approval_policy=approval_policy,
        audit=audit,
    )


async def collect(executor: PlanExecutor, goal: str) -> list[PlanEvent]:
    return [e async for e in executor.run(goal)]


class TestExecutorFlow:
    async def test_full_flow_event_sequence(self, settings):
        """拆解 → 审阅 → 批次执行 → plan_done 的事件顺序。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={
                "t1": [("t1 完成", [])],
                "t2": [("t2 完成", [])],
            },
        )
        ex = make_executor(llm, settings, reviewer=review_execute)
        events = await collect(ex, "做点事")

        kinds = [e.kind for e in events]
        assert kinds == [
            "plan_generated", "review", "approved",
            "batch_started", "subtask_started", "subtask_event", "subtask_done",
            "batch_started", "subtask_started", "subtask_event", "subtask_done",
            "plan_done",
        ]
        assert [e.message for e in events if e.kind == "batch_started"] == [
            "第 1 轮 / 共 2 轮",
            "第 2 轮 / 共 2 轮",
        ]
        assert events[-1].message.startswith("计划完成: 2/2")

    async def test_subtask_tool_execution(self, settings, tmp_project):
        """子任务内调用工具：结果回灌并体现在最终摘要。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={
                "t1": [
                    ("", [("read_file", '{"path": "README.md"}')]),
                    ("读到 demo", []),
                ],
                "t2": [("t2 完成", [])],
            },
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "读 README")

        # 子任务内部事件被转发（tool_call / tool_result）
        inner = [e for e in events if e.kind == "subtask_event"]
        assert any(e.agent_event.kind == "tool_call" for e in inner)
        tool_results = [e for e in inner if e.agent_event.kind == "tool_result"]
        assert tool_results and all(e.agent_event.tool_result.ok for e in tool_results)
        t1_done = [e for e in events if e.kind == "subtask_done" and e.task.id == "t1"]
        assert t1_done[0].message == "读到 demo"

    async def test_cancel_executes_no_tools(self, settings, tmp_project):
        """审阅取消：不执行任何子任务。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={"t1": [("不应执行", [])], "t2": [("不应执行", [])]},
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_cancel)
        events = await collect(ex, "做点事")

        kinds = [e.kind for e in events]
        assert kinds == ["plan_generated", "review", "cancelled"]
        # 除规划调用外没有任何 LLM / 工具调用
        assert len(llm.requests) == 1

    async def test_fail_closed_without_reviewer(self, settings):
        """无审阅回调：fail closed 自动取消。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={"t1": [("不应执行", [])], "t2": [("不应执行", [])]},
        )
        ex = make_executor(llm, settings, reviewer=None)
        events = await collect(ex, "做点事")

        kinds = [e.kind for e in events]
        assert kinds == ["plan_generated", "cancelled"]
        assert "fail closed" in events[-1].message
        assert len(llm.requests) == 1

    async def test_replan_with_feedback(self, settings):
        """重规划：feedback 与旧计划回传，按新计划执行。"""
        new_plan = _plan_json([
            {"id": "t1", "title": "新版任务", "description": "重新拆解", "deps": []},
        ])
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN, new_plan],
            subtask_script={"t1": [("新版完成", [])]},
        )

        seen: list[ReviewDecision] = []

        async def reviewer(plan):
            if not seen:
                seen.append(ReviewDecision(action="replan", feedback="把测试改成 lint"))
                return seen[0]
            return ReviewDecision(action="execute")

        ex = make_executor(llm, settings, reviewer=reviewer)
        events = await collect(ex, "加个工具函数")

        kinds = [e.kind for e in events]
        assert kinds[:4] == ["plan_generated", "review", "replanned", "plan_generated"]
        assert kinds[-1] == "plan_done"
        # 第二次规划请求包含 feedback 与上一版计划
        second = llm.requests[1]
        all_text = "\n".join(m.content for m in second)
        assert "把测试改成 lint" in all_text
        assert "上一版计划" in all_text
        # 执行的是新计划（t1 标题为新版任务）
        done_events = [e for e in events if e.kind == "subtask_done"]
        assert done_events[0].task.title == "新版任务"
        assert done_events[0].message == "新版完成"

    async def test_generation_retry_on_bad_json(self, settings):
        """非法 JSON → 带错误信息重试 → 成功。"""
        llm = PlanScriptedClient(
            plan_script=["不是 JSON", "```json\n" + VALID_PLAN + "\n```"],
            subtask_script={"t1": [("t1 完成", [])], "t2": [("t2 完成", [])]},
        )
        ex = make_executor(llm, settings, reviewer=review_execute)
        events = await collect(ex, "做点事")

        assert events[-1].kind == "plan_done"
        assert len(llm.requests) == 4  # 规划 2 次 + 子任务 2 次
        # 第二次规划请求带上错误信息
        second = llm.requests[1]
        assert any("解析失败" in m.content for m in second)

    async def test_generation_failure_after_retries(self, settings):
        """重试用尽 → plan_failed，提示改用 ReAct。"""
        llm = PlanScriptedClient(
            plan_script=["坏输出"] * 3,
            subtask_script={},
        )
        ex = make_executor(llm, settings, reviewer=review_execute)
        events = await collect(ex, "做点事")

        assert [e.kind for e in events] == ["plan_failed"]
        assert "ReAct" in events[-1].message
        assert len(llm.requests) == 3  # 1 次初始 + 2 次重试


class TestExecutorContext:
    async def test_dep_result_injected(self, settings, tmp_project):
        """依赖结果注入：t2 的请求上下文包含 t1 的结果。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={
                "t1": [("t1 的执行结果摘要", [])],
                "t2": [("t2 完成", [])],
            },
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        await collect(ex, "做点事")

        # 请求序：0=规划, 1=t1, 2=t2
        t2_request = llm.requests[2]
        system = t2_request[0].content
        assert "总目标" in system
        assert "t1 的执行结果摘要" in system
        assert "已完成的子任务" in system

    async def test_failed_dep_injected(self, settings, tmp_project):
        """失败传播：t1 失败 → t2 上下文含 [子任务 t1 失败]。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={
                "t1": [LlmError("上游服务挂了")],
                "t2": [("t2 调整后完成", [])],
            },
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "做点事")

        failed = [e for e in events if e.kind == "subtask_failed"]
        assert failed and failed[0].task.id == "t1"
        # 失败后计划仍继续（默认允许 3 次失败）
        assert events[-1].kind == "plan_done"
        t2_request = llm.requests[2]
        system = t2_request[0].content
        assert "[子任务 t1 失败]" in system
        assert "上游服务挂了" in system

    async def test_failure_limit_terminates(self, settings, tmp_project):
        """失败超限：终止剩余批次，plan_failed。"""
        plan = _plan_json([
            {"id": "t1", "title": "失败任务", "description": "-", "deps": []},
            {"id": "t2", "title": "成功任务", "description": "-", "deps": []},
            {"id": "t3", "title": "不再执行", "description": "-", "deps": ["t2"]},
            {"id": "t4", "title": "同样不执行", "description": "-", "deps": ["t1"]},
        ])
        llm = PlanScriptedClient(
            plan_script=[plan],
            subtask_script={
                "t1": [LlmError("boom")],
                "t2": [("t2 完成", [])],
                "t3": [("不应执行", [])],
                "t4": [("不应执行", [])],
            },
        )
        settings = Settings(
            api_base="https://api.test/v1", api_key="sk-test", model="m",
            plan_max_failures=0, plan_subtask_steps=2,
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "做点事")

        assert events[-1].kind == "plan_failed"
        assert "超过上限" in events[-1].message
        # 批次 1 = [t1, t2] 全部执行；失败 1 > 0 → 批次 2 = [t3, t4] 终止
        executed = [e.task.id for e in events if e.kind == "subtask_started"]
        assert executed == ["t1", "t2"]
        assert "t3" in events[-1].message and "t4" in events[-1].message

    async def test_subtask_step_limit_fails(self, settings, tmp_project):
        """子任务步数用尽 → 标记失败。"""
        llm = PlanScriptedClient(
            plan_script=[_plan_json([{"id": "t1", "title": "跑不完", "description": "-", "deps": []}])],
            subtask_script={"t1": [("", [("read_file", '{"path": "README.md"}')])] * 10},
        )
        settings = Settings(
            api_base="https://api.test/v1", api_key="sk-test", model="m",
            plan_subtask_steps=2,
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "做点事")

        failed = [e for e in events if e.kind == "subtask_failed"]
        assert failed and "步数上限" in failed[0].message


class TestExecutorParallel:
    async def test_batch_parallel_timing(self, settings, tmp_project):
        """同批子任务并行：耗时接近单批最慢项而非求和。"""
        delay = 0.3

        def slow_handler(args: dict) -> ToolResult:
            time.sleep(delay)
            return ToolResult(tool_call_id="", name="slow_tool", ok=True, output="慢工具完成")

        registry = ToolRegistry()
        registry.register(Tool(
            name="slow_tool",
            description="慢工具",
            parameters={"type": "object", "properties": {}},
            handler=slow_handler,
        ))

        plan = _plan_json([
            {"id": "t1", "title": "并行A", "description": "-", "deps": []},
            {"id": "t2", "title": "并行B", "description": "-", "deps": []},
            {"id": "t3", "title": "收尾", "description": "-", "deps": ["t1", "t2"]},
        ])
        llm = PlanScriptedClient(
            plan_script=[plan],
            subtask_script={
                "t1": [("", [("slow_tool", "{}")]), ("A 完成", [])],
                "t2": [("", [("slow_tool", "{}")]), ("B 完成", [])],
                "t3": [("C 完成", [])],
            },
        )
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)

        started = time.monotonic()
        events = await collect(ex, "并行任务")
        elapsed = time.monotonic() - started

        assert events[-1].kind == "plan_done"
        # 串行两个慢工具至少 0.6s，并行应 < 0.55s（留裕量）
        assert elapsed < delay * 2 - 0.05, f"同批未并行执行: {elapsed:.2f}s"

    async def test_batches_execute_in_order(self, settings, tmp_project):
        """跨批次严格按拓扑顺序：t2 必须在 t1 完成后启动。"""
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={
                "t1": [("t1 完成", [])],
                "t2": [("t2 完成", [])],
            },
        )
        registry = build_registry(base_dir=tmp_project)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "做点事")

        started = [e.task.id for e in events if e.kind == "subtask_started"]
        done = [e.task.id for e in events if e.kind == "subtask_done"]
        assert started == ["t1", "t2"]
        assert done == ["t1", "t2"]


class TestExecutorSafety:
    async def test_guard_blocks_outside_path(self, settings, tmp_project):
        """子任务内路径越界仍被策略层拒绝（不可绕过）。"""
        llm = PlanScriptedClient(
            plan_script=[_plan_json([{"id": "t1", "title": "越界写", "description": "-", "deps": []}])],
            subtask_script={
                "t1": [
                    ("", [("write_file", json.dumps({"path": "../outside.txt", "content": "x"}))]),
                    ("被拒绝，我调整", []),
                ],
            },
        )
        registry = build_registry(base_dir=tmp_project, guard=lambda n, a: guard_tool_call(tmp_project, n, a))
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute)
        events = await collect(ex, "越界任务")

        inner = [e.agent_event for e in events if e.kind == "subtask_event"]
        denied = [r for r in (e.tool_result for e in inner if e.kind == "tool_result") if r and not r.ok]
        assert denied and "策略拒绝" in denied[0].error
        # 子任务本身以模型汇报结束（拒绝信息已回灌）
        assert events[-1].kind == "plan_done"

    async def test_hitl_fail_closed_in_subtask(self, settings, tmp_project):
        """子任务内危险操作仍走 HITL：无回调时 fail closed 拒绝。"""
        llm = PlanScriptedClient(
            plan_script=[_plan_json([{"id": "t1", "title": "写文件", "description": "-", "deps": []}])],
            subtask_script={
                "t1": [
                    ("", [("write_file", '{"path": "out.txt", "content": "x"}')]),
                    ("被拒，放弃", []),
                ],
            },
        )
        registry = build_registry(base_dir=tmp_project)
        policy = HITLPolicy(enabled=True)  # 无 requester → fail closed
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute, approval_policy=policy)
        events = await collect(ex, "写文件")

        inner = [e.agent_event for e in events if e.kind == "subtask_event"]
        approvals = [e for e in inner if e.kind == "approval"]
        assert approvals and approvals[0].text == "rejected"
        rejected_results = [
            e for e in inner
            if e.kind == "tool_result" and e.tool_result and "USER_REJECTED" in e.tool_result.error
        ]
        assert rejected_results
        assert not (tmp_project / "out.txt").exists()

    async def test_audit_records_subtask_events(self, settings, tmp_project):
        """审计日志包含 subtask_started / subtask_done。"""
        audit = SpyAudit()
        llm = PlanScriptedClient(
            plan_script=[VALID_PLAN],
            subtask_script={"t1": [("t1 完成", [])], "t2": [("t2 完成", [])]},
        )
        registry = build_registry(base_dir=tmp_project, audit=audit)
        ex = make_executor(llm, settings, registry=registry, reviewer=review_execute, audit=audit)
        await collect(ex, "做点事")

        assert "subtask_started" in audit.actions
        assert "subtask_done" in audit.actions


async def collect_resume(executor: PlanExecutor, instruction: str = "") -> list[PlanEvent]:
    return [e async for e in executor.resume(instruction)]


class TestExecutorResume:
    async def test_resume_without_plan_fails(self, settings):
        """尚未执行/生成计划就 resume → plan_failed。"""
        llm = PlanScriptedClient(plan_script=[], subtask_script={})
        ex = make_executor(llm, settings, reviewer=review_execute)
        events = await collect_resume(ex)
        assert [e.kind for e in events] == ["plan_failed"]
        assert "没有可恢复" in events[0].message

    async def test_resume_reruns_failed_skips_done(self, settings):
        """断点续跑：重跑失败的 t1、跳过已完成的 t2、继续未执行的 t3。"""
        settings = Settings(
            api_base="https://api.test/v1", api_key="sk-test", model="m",
            plan_max_failures=0,
        )
        plan = _plan_json([
            {"id": "t1", "title": "易失败", "description": "-", "deps": []},
            {"id": "t2", "title": "已成功", "description": "-", "deps": []},
            {"id": "t3", "title": "依赖t1", "description": "-", "deps": ["t1"]},
        ])
        llm = PlanScriptedClient(
            plan_script=[plan],
            subtask_script={
                "t1": [LlmError("boom")],
                "t2": [("t2 完成", [])],
            },
        )
        ex = make_executor(llm, settings, reviewer=review_execute)
        events = await collect(ex, "做点事")
        assert events[-1].kind == "plan_failed"
        assert "t3" in events[-1].message  # 剩余子任务 t3 被终止
        assert not any(e.kind == "subtask_started" and e.task.id == "t3" for e in events)

        # 恢复：t1 修复成功，t2 保留结果，t3 继续执行
        llm.subtask_script["t1"] = [("t1 修复后完成", [])]
        llm.subtask_script["t3"] = [("t3 完成", [])]
        resume_events = await collect_resume(ex)
        started = [e.task.id for e in resume_events if e.kind == "subtask_started"]
        assert any(e.kind == "plan_resume_requested" for e in resume_events)
        assert started == ["t1", "t3"]  # t2 已 done，跳过
        assert resume_events[-1].kind == "plan_done"
        # 失败计数已重置（0 > 0 不触发，且 t1 本次成功）
        assert sum(t.status == "done" for t in ex._last_plan.tasks) == 3

    async def test_resume_injects_instruction(self, settings):
        """补充指令被注入到被重跑子任务的 user 消息。"""
        settings = Settings(
            api_base="https://api.test/v1", api_key="sk-test", model="m",
            plan_max_failures=0, plan_subtask_steps=5,
        )
        plan = _plan_json([{"id": "t1", "title": "失败", "description": "-", "deps": []}])
        llm = PlanScriptedClient(plan_script=[plan], subtask_script={"t1": [LlmError("boom")]})
        ex = make_executor(llm, settings, reviewer=review_execute)
        await collect(ex, "做点事")

        llm.subtask_script["t1"] = [("t1 完成", [])]
        await collect_resume(ex, "改用 uv 安装依赖")
        t1_request = llm.requests[-1]
        user = next(m.content for m in t1_request if m.role == "user")
        assert "改用 uv 安装依赖" in user
