"""Plan-and-Execute：任务拆解 → DAG 批次 → 审阅 → 批次执行（第 4 期）。

ReAct 之外的第二条执行路径：
- LLM 独立调用拆解任务为「子任务 + 依赖」，结构化 JSON 输出 + 校验/修复/重试
- Kahn 拓扑排序生成依赖批次，无依赖子任务按批并行
- 计划审阅回调（fail closed：无回调自动取消）
- 子任务以迷你 ReAct 循环执行（复用并行工具 / HITL / 策略层 / 审计）
- 失败传播：子任务失败以错误信息注入依赖方上下文，失败超限终止剩余批次
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Literal

from routivus.agent.react import AgentEvent, DEFAULT_SYSTEM_PROMPT, ReActAgent
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient, LlmError
from routivus.llm.types import Message, Usage
from routivus.memory.context import ConversationContext
from routivus.memory.manager import MemoryManager
from routivus.safety.hitl import HITLPolicy
from routivus.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from routivus.mcp.manager import McpManager

# 依赖结果摘要注入上限（字符）
DEP_RESULT_LIMIT = 2000
# 子任务结果摘要上限（字符）
TASK_RESULT_LIMIT = 2000
# 拆解重试上限（带错误信息重试次数）
PLAN_MAX_RETRIES = 2

PLANNER_SYSTEM_PROMPT = (
    "你是任务规划器。将用户任务拆解为可执行的子任务列表并识别依赖关系，"
    "只输出一个 JSON 对象，不要输出任何其他文本或 markdown 代码块，格式：\n"
    '{"tasks": [{"id": "t1", "title": "一句话标题", '
    '"description": "执行说明，写明要调用的工具", "deps": []}]}\n'
    "规则：\n"
    "- id 形如 t1/t2/t3，全局唯一\n"
    "- deps 只能引用其他子任务的 id，无依赖用空数组\n"
    "- 依赖关系必须是无环的 DAG\n"
    "- description 写清楚执行步骤与需要调用的工具（write_file / execute_command 等）"
)


class PlanError(Exception):
    """计划生成 / 校验失败（消息面向用户可直接展示）。"""


# ---------- 数据模型 ----------


@dataclass
class PlanTask:
    """计划中的一个子任务。"""

    id: str                  # "t1" / "t2" ...
    title: str               # 一句话标题
    description: str         # 执行说明（含要调用的工具）
    deps: list[str]          # 依赖的 task id（可为空）
    status: str = "pending"  # pending / running / done / failed
    result: str = ""         # 执行结果摘要


@dataclass
class Plan:
    """一份可执行的计划：目标 + 子任务 + 拓扑批次。"""

    goal: str
    tasks: list[PlanTask]
    batches: list[list[str]]  # 拓扑排序后的批次（按 id）

    def task_by_id(self, tid: str) -> PlanTask | None:
        for t in self.tasks:
            if t.id == tid:
                return t
        return None


@dataclass
class PlanEvent:
    """计划执行事件流单元。kind 含义：

    - plan_generated: 拆解完成（plan 携带完整计划）
    - review: 即将进入审阅（等待回调决策）
    - approved / cancelled / replanned: 审阅决策
    - batch_started: 一个依赖批次开始（batch 为该批 task id）
    - subtask_started / subtask_done / subtask_failed: 子任务生命周期
    - subtask_event: 子任务内部转发的 AgentEvent（agent_event 字段）
    - plan_done / plan_failed: 计划结束（汇总）
    """

    kind: Literal[
        "plan_generated", "review", "approved", "cancelled", "replanned",
        "batch_started", "subtask_started", "subtask_done", "subtask_failed",
        "subtask_event", "planner_usage", "plan_done", "plan_failed",
        "plan_resume_requested",
    ]
    plan: Plan | None = None
    batch: list[str] = field(default_factory=list)
    task: PlanTask | None = None
    message: str = ""
    agent_event: AgentEvent | None = None
    usage: Usage | None = None
    estimated_prompt_tokens: int | None = None
    request_token_limit: int | None = None
    context_window: int | None = None
    compaction_before: int | None = None
    compaction_after: int | None = None


@dataclass
class ReviewDecision:
    """计划审阅决策。"""

    action: Literal["execute", "cancel", "replan"]
    feedback: str = ""  # replan 时的补充要求


PlanReviewer = Callable[[Plan], Awaitable[ReviewDecision]]


# ---------- DAG → 依赖批次（Kahn 算法） ----------


def build_batches(tasks: list[PlanTask]) -> list[list[str]]:
    """拓扑排序为依赖批次。批次 0 = 无依赖子任务；批次 n = 依赖全部在前 n-1 批完成的子任务。

    依赖存在环时抛出 PlanError。deps 引用了不存在的 id 视为无法满足，同样按环处理。
    """
    remaining = {t.id: set(t.deps) for t in tasks}
    batches: list[list[str]] = []
    while remaining:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            raise PlanError("依赖存在环（或引用了不存在的任务 id），无法生成执行轮次")
        batches.append(sorted(ready))
        for tid in ready:
            remaining.pop(tid)
        for deps in remaining.values():
            deps.difference_update(ready)
    return batches


# ---------- 拆解输出解析：校验 + 自动修复 ----------

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


def _extract_json_object(text: str) -> str:
    """提取最外层 { ... } 片段（容忍前后夹杂解释文本）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def parse_tasks(raw: str, max_subtasks: int = 12) -> tuple[list[PlanTask], list[str]]:
    """解析 LLM 拆解输出为 PlanTask 列表。

    返回 (tasks, warnings)；解析失败抛 PlanError（消息带原因，供重试回灌）。
    自动修复：未知 dep / 自依赖移除并告警；超上限截断并告警。
    """
    warnings: list[str] = []
    text = _extract_json_object(_strip_fences(raw))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f"JSON 解析失败: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise PlanError('顶层结构必须是 {"tasks": [...]}')
    raw_tasks = data["tasks"]
    if not raw_tasks:
        raise PlanError("tasks 为空，至少需要一个子任务")

    tasks: list[PlanTask] = []
    for i, rt in enumerate(raw_tasks):
        if not isinstance(rt, dict):
            raise PlanError(f"tasks[{i}] 必须是对象")
        tid = str(rt.get("id", "")).strip()
        title = str(rt.get("title", "")).strip()
        if not tid or not title:
            raise PlanError(f"tasks[{i}] 缺少 id 或 title 字段")
        description = str(rt.get("description") or "").strip() or title
        deps_raw = rt.get("deps", [])
        if not isinstance(deps_raw, list):
            raise PlanError(f"tasks[{i}].deps 必须是数组")
        # 去重去空，保持顺序
        deps: list[str] = []
        for d in deps_raw:
            ds = str(d).strip()
            if ds and ds not in deps:
                deps.append(ds)
        tasks.append(PlanTask(id=tid, title=title, description=description, deps=deps))

    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise PlanError("存在重复的子任务 id")

    if len(tasks) > max_subtasks:
        warnings.append(f"子任务数 {len(tasks)} 超过上限 {max_subtasks}，已截断")
        kept = {t.id for t in tasks[:max_subtasks]}
        tasks = tasks[:max_subtasks]
        for t in tasks:
            t.deps = [d for d in t.deps if d in kept]

    known = {t.id for t in tasks}
    for t in tasks:
        for d in list(t.deps):
            if d == t.id:
                warnings.append(f"子任务 {t.id} 自依赖，已移除")
                t.deps.remove(d)
            elif d not in known:
                warnings.append(f"子任务 {t.id} 引用了不存在的依赖 {d}，已移除")
                t.deps.remove(d)

    try:
        build_batches(tasks)
    except PlanError as e:
        raise PlanError(f"{e}，请把依赖关系调整为 DAG") from e
    return tasks, warnings


# ---------- 计划执行器 ----------


class PlanExecutor:
    """拆解 → 审阅 → 按批次执行 的事件流编排。"""

    def __init__(
        self,
        llm: LlmClient,
        tools: ToolRegistry,
        settings: Settings,
        reviewer: PlanReviewer | None = None,
        approval_policy: HITLPolicy | None = None,
        audit=None,
        memory_manager: MemoryManager | None = None,
        mcp_manager: "McpManager | None" = None,
        ask_requester=None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.reviewer = reviewer
        self.approval_policy = approval_policy
        self.audit = audit
        self.memory_manager = memory_manager
        self.mcp_manager = mcp_manager
        self.ask_requester = ask_requester
        self._planner_context_event: AgentEvent | None = None
        self._planner_usage: Usage | None = None
        self._last_plan: Plan | None = None

    async def run(self, goal: str) -> AsyncIterator[PlanEvent]:
        """执行完整流程：拆解 → 审阅（可循环重规划）→ 按批次执行 → 汇总。"""
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.ensure_started()
                goal = await self.mcp_manager.expand_references(goal)
            except Exception as exc:
                yield PlanEvent(kind="plan_failed", message=f"MCP resource 处理失败: {exc}")
                return
        feedback = ""
        previous: Plan | None = None

        # ---- 拆解 + 审阅（重规划时循环） ----
        while True:
            try:
                plan, warnings = await self._generate_plan(goal, feedback, previous)
            except LlmError as e:
                yield PlanEvent(kind="plan_failed", message=f"计划生成失败: {e}")
                return
            if plan is None:
                yield PlanEvent(
                    kind="plan_failed",
                    message="计划生成失败（JSON 解析重试已用尽），建议改用 ReAct 模式直接执行任务。",
                )
                return
            planner_context = self._planner_context_event
            yield PlanEvent(
                kind="plan_generated", plan=plan, message="；".join(warnings),
                usage=self._planner_usage,
                estimated_prompt_tokens=(
                    planner_context.estimated_prompt_tokens
                    if planner_context is not None else None
                ),
                request_token_limit=(
                    planner_context.request_token_limit
                    if planner_context is not None else None
                ),
                context_window=(
                    planner_context.context_window
                    if planner_context is not None else None
                ),
                compaction_before=(
                    planner_context.compaction_before
                    if planner_context is not None else None
                ),
                compaction_after=(
                    planner_context.compaction_after
                    if planner_context is not None else None
                ),
            )

            if self.reviewer is None:
                # fail closed：无审阅回调，自动取消，不执行任何工具
                yield PlanEvent(kind="cancelled", plan=plan, message="无审阅回调，计划自动取消（fail closed）")
                return

            yield PlanEvent(kind="review", plan=plan, message="等待用户审阅")
            decision = await self.reviewer(plan)

            if decision.action == "execute":
                self._last_plan = plan
                yield PlanEvent(kind="approved", plan=plan)
                break
            if decision.action == "cancel":
                yield PlanEvent(kind="cancelled", plan=plan, message="用户取消计划")
                return
            # replan：带 feedback 重新拆解
            yield PlanEvent(kind="replanned", plan=plan, message=decision.feedback)
            feedback = decision.feedback
            previous = plan

        # ---- 按批次执行 ----
        async for event in self._execute_batches(plan):
            yield event

    async def _execute_batches(self, plan: Plan, instruction: str = "") -> AsyncIterator[PlanEvent]:
        """按已排序批次执行子任务；失败超限终止剩余批次。instruction 注入每个被执行子任务。"""
        failures = 0
        for batch_no, batch in enumerate(plan.batches):
            yield PlanEvent(
                kind="batch_started", plan=plan, batch=batch,
                message=f"第 {batch_no + 1} 轮 / 共 {len(plan.batches)} 轮",
            )
            async for event in self._run_batch(plan, batch, instruction=instruction):
                if event.kind == "subtask_failed":
                    failures += 1
                yield event
            if failures > self.settings.plan_max_failures:
                remaining = [tid for b in plan.batches[batch_no + 1:] for tid in b]
                yield PlanEvent(
                    kind="plan_failed", plan=plan,
                    message=(
                        f"失败子任务数 {failures} 超过上限 {self.settings.plan_max_failures}，"
                        f"终止剩余子任务: {', '.join(remaining) if remaining else '无'}"
                    ),
                )
                return

        done = sum(1 for t in plan.tasks if t.status == "done")
        yield PlanEvent(
            kind="plan_done", plan=plan,
            message=f"计划完成: {done}/{len(plan.tasks)} 个子任务成功",
        )

    # ---- 断点续跑（V3）----

    async def resume(self, instruction: str = "") -> AsyncIterator[PlanEvent]:
        """从失败/未执行子任务断点续跑。计划已审阅批准过，跳过拆解与审阅。"""
        plan = self._last_plan
        if plan is None:
            yield PlanEvent(kind="plan_failed", message="没有可恢复的计划")
            return
        done = sum(1 for t in plan.tasks if t.status == "done")
        if done == len(plan.tasks):
            yield PlanEvent(kind="plan_failed", plan=plan, message="计划已全部完成，无需恢复")
            return
        # 重置失败/运行中/待办子任务为 pending（done 保留 result 供下游复用）
        for task in plan.tasks:
            if task.status in ("failed", "running", "pending"):
                task.status = "pending"
                task.result = ""
        plan.batches = build_batches(plan.tasks)
        skipped = [t.id for t in plan.tasks if t.status == "done"]
        yield PlanEvent(
            kind="plan_resume_requested", plan=plan,
            message=(
                f"恢复执行：跳过 {len(skipped)} 个已完成子任务，"
                f"重跑 {sum(1 for b in plan.batches for _ in b)} 个子任务"
                + (f"；补充指令：{instruction}" if instruction else "")
            ),
        )
        async for event in self._execute_batches(plan, instruction=instruction):
            yield event

    # ---- 拆解（LLM 结构化输出 + 重试） ----

    async def _generate_plan(
        self, goal: str, feedback: str, previous: Plan | None
    ) -> tuple[Plan | None, list[str]]:
        """调用 LLM 生成计划。解析失败带错误信息重试（上限 PLAN_MAX_RETRIES 次）。

        返回 (plan, warnings)；重试用尽返回 (None, warnings)。
        """
        self._planner_context_event = None
        self._planner_usage = None
        planner_context = ConversationContext(
            PLANNER_SYSTEM_PROMPT,
            self.settings,
            shared_provider=self.memory_manager.shared_sections if self.memory_manager else None,
        )
        planner_context.append(
            Message(role="user", content=self._planner_prompt(goal, feedback, previous))
        )
        budget = await planner_context.ensure_budget(self.llm)
        self._planner_context_event = AgentEvent(
            kind="context_usage",
            estimated_prompt_tokens=budget.after_tokens,
            request_token_limit=budget.request_token_limit,
            context_window=self.settings.context_window,
            compaction_before=(
                budget.before_tokens if budget.status == "compacted" else None
            ),
            compaction_after=(
                budget.after_tokens if budget.status == "compacted" else None
            ),
        )
        if not budget.proceed:
            raise LlmError(budget.message or "规划上下文超出模型窗口")
        messages = planner_context.build_messages()
        warnings: list[str] = []
        for _attempt in range(1 + PLAN_MAX_RETRIES):
            raw, usage = await self._llm_text(messages)
            self._planner_usage = usage
            try:
                tasks, warns = parse_tasks(raw, self.settings.plan_max_subtasks)
            except PlanError as e:
                messages.append(Message(role="assistant", content=raw))
                messages.append(Message(
                    role="user",
                    content=f"上面的输出解析失败：{e}。请修复问题并重新输出完整 JSON（不要包含其他文本）。",
                ))
                continue
            warnings.extend(warns)
            return Plan(goal=goal, tasks=tasks, batches=build_batches(tasks)), warnings
        return None, warnings

    def _planner_prompt(self, goal: str, feedback: str, previous: Plan | None) -> str:
        parts = [f"任务：{goal}"]
        if previous is not None:
            titles = "\n".join(f"- {t.id}: {t.title}" for t in previous.tasks)
            parts.append(f"上一版计划（需要调整）：\n{titles}")
        if feedback:
            parts.append(f"用户反馈（重新拆解时必须满足）：{feedback}")
        parts.append(f"请拆解为不超过 {self.settings.plan_max_subtasks} 个子任务，输出严格 JSON。")
        return "\n\n".join(parts)

    async def _llm_text(self, messages: list[Message]) -> tuple[str, Usage | None]:
        """非流式语义：聚合一次 LLM 调用的全部文本。"""
        parts: list[str] = []
        usage: Usage | None = None
        async for event in self.llm.stream_chat(messages, tools=None):
            if event.kind == "content" and event.text:
                parts.append(event.text)
            elif event.kind == "done":
                usage = event.usage
        return "".join(parts), usage

    # ---- 批次执行（批内并行） ----

    async def _run_batch(self, plan: Plan, batch: list[str], instruction: str = "") -> AsyncIterator[PlanEvent]:
        """并发执行本批子任务，事件按到达顺序转发（含子任务内部 AgentEvent）。

        断点续跑时跳过已完成（done）的子任务，避免重复执行。
        """
        pending = [tid for tid in batch if plan.task_by_id(tid).status != "done"]
        if not pending:
            return
        queue: asyncio.Queue[PlanEvent | None] = asyncio.Queue()

        async def runner(tid: str) -> None:
            try:
                await self._run_one(plan, batch, tid, queue, instruction=instruction)
            finally:
                queue.put_nowait(None)

        gather_task = asyncio.gather(*(runner(tid) for tid in pending), return_exceptions=True)
        completed = 0
        while completed < len(pending):
            item = await queue.get()
            if item is None:
                completed += 1
                continue
            yield item
        # gather 仅用于收集异常（runner 内部已兜底，这里防御性回灌）
        for result in await gather_task:
            if isinstance(result, Exception):
                yield PlanEvent(kind="subtask_failed", plan=plan, message=f"内部错误: {result}")

    async def _run_one(
        self, plan: Plan, batch: list[str], tid: str, queue: "asyncio.Queue[PlanEvent | None]",
        instruction: str = "",
    ) -> None:
        task = plan.task_by_id(tid)
        assert task is not None
        task.status = "running"
        self._audit("subtask_started", task_id=task.id, title=task.title)
        queue.put_nowait(PlanEvent(kind="subtask_started", plan=plan, batch=batch, task=task))

        result, error = "", ""
        try:
            result, error = await self._execute_subtask(plan, task, queue, instruction=instruction)
        except Exception as e:  # 防御：子任务内部异常不拖垮整批
            error = f"{type(e).__name__}: {e}"

        if error:
            task.status = "failed"
            task.result = error[:TASK_RESULT_LIMIT]
            self._audit("subtask_failed", task_id=task.id, error=task.result)
            queue.put_nowait(PlanEvent(
                kind="subtask_failed", plan=plan, batch=batch, task=task, message=task.result
            ))
        else:
            task.status = "done"
            task.result = result[:TASK_RESULT_LIMIT]
            self._audit("subtask_done", task_id=task.id, result=task.result)
            queue.put_nowait(PlanEvent(
                kind="subtask_done", plan=plan, batch=batch, task=task, message=task.result
            ))

    async def _execute_subtask(
        self, plan: Plan, task: PlanTask, queue: "asyncio.Queue[PlanEvent | None]",
        instruction: str = "",
    ) -> tuple[str, str]:
        """迷你 ReAct 循环执行单个子任务。返回 (result, error)，error 非空即失败。"""
        sub_settings = replace(self.settings, tool_steps=self.settings.plan_subtask_steps)
        agent = ReActAgent(
            llm=self.llm,
            tools=self.tools,
            settings=sub_settings,
            system_prompt=self._subtask_system_prompt(plan, task),
            approval_policy=self.approval_policy,
            audit=self.audit,
            memory_manager=self.memory_manager,
            mcp_manager=self.mcp_manager,
            ask_requester=self.ask_requester,
        )
        async for event in agent.run(self._subtask_user_prompt(task, instruction)):
            if event.kind in (
                "thinking", "content", "tool_call", "ask_user", "approval", "tool_result",
                "context_compacted", "context_warning", "context_usage", "usage",
            ):
                queue.put_nowait(PlanEvent(
                    kind="subtask_event", plan=plan, task=task, agent_event=event
                ))
            elif event.kind == "error":
                return "", f"LLM 请求失败: {event.text}"
            elif event.kind == "done":
                if event.usage is not None:
                    queue.put_nowait(PlanEvent(
                        kind="subtask_event", plan=plan, task=task,
                        agent_event=AgentEvent(
                            kind="usage", usage=event.usage,
                            estimated_prompt_tokens=event.estimated_prompt_tokens,
                            request_token_limit=event.request_token_limit,
                            context_window=event.context_window,
                            compaction_before=event.compaction_before,
                            compaction_after=event.compaction_after,
                        ),
                    ))
            elif event.kind == "step_limit":
                return "", f"达到子任务步数上限（{self.settings.plan_subtask_steps}）"
            elif event.kind in ("budget_exceeded", "context_overflow"):
                return "", event.text or "子任务上下文 token 超限"
        # 成功：取最后一条含内容的 assistant 消息作为结果摘要
        final = ""
        for m in reversed(agent.messages):
            if m.role == "assistant" and m.content.strip():
                final = m.content
                break
        return final, ""

    # ---- 子任务上下文注入 ----

    def _subtask_system_prompt(self, plan: Plan, task: PlanTask) -> str:
        parts = [
            DEFAULT_SYSTEM_PROMPT,
            "",
            "# 计划上下文",
            f"总目标：{plan.goal}",
        ]
        skill_registry = getattr(self.tools, "skill_registry", None)
        if skill_registry is not None:
            index = skill_registry.index_text()
            if index:
                parts.extend(["", index])
        completed = [t for t in plan.tasks if t.status == "done"]
        if completed:
            parts.append("已完成的子任务：\n" + "\n".join(f"- {t.id}: {t.title}" for t in completed))
        dep_sections: list[str] = []
        for d in task.deps:
            dep = plan.task_by_id(d)
            if dep is None:
                continue
            if dep.status == "failed":
                dep_sections.append(f"[子任务 {dep.id} 失败] {dep.result or '无详细信息'}")
            else:
                dep_sections.append(
                    f"[{dep.id} {dep.title}] 结果：\n{dep.result[:DEP_RESULT_LIMIT]}"
                )
        if dep_sections:
            parts.append("依赖子任务的结果：\n" + "\n\n".join(dep_sections))
        return "\n".join(parts)

    def _subtask_user_prompt(self, task: PlanTask, instruction: str = "") -> str:
        base = (
            f"请执行子任务 {task.id}（{task.title}）：\n{task.description}\n\n"
            "只执行这一个子任务，不要处理计划中的其他子任务。"
            "完成后用一小段话汇报执行结果。"
        )
        if instruction:
            base = f"{base}\n\n恢复时补充指令：{instruction}"
        return base

    def _audit(self, action: str, **fields) -> None:
        if self.audit is not None:
            self.audit.record(action, **fields)
