"""UI-independent orchestration for one interactive XG session."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Literal

from xg.agent.plan import Plan, PlanEvent, PlanExecutor, ReviewDecision
from xg.agent.react import AgentEvent, ReActAgent
from xg.agent.team import ResourceClaim, TeamEvent, TeamExecutor, TeamPlan
from xg.adaptive.feedback import FeedbackRecorder
from xg.adaptive.signals import capture_turn_signals
from xg.cli.commands import CommandContext, CommandResult, CommandService
from xg.cli.help import parse_help_command
from xg.config.manager import ConfigManager
from xg.config.settings import Settings
from xg.llm.types import Message
from xg.router import TIER_NAMES, resolve as resolve_tier, route as route_turn
from xg.safety.hitl import ApprovalDecision
from xg.tui.reducer import (
    finalize_trace,
    reduce_agent_event,
    reduce_plan_event,
    reduce_team_event,
    set_smart_router_snapshot,
)
from xg.tui.state import (
    ApprovalRequest,
    ConfirmationRequest,
    InspectorView,
    QueueItem,
    QueueItemKind,
    MemoryInspectorSnapshot,
    SafetyInspectorSnapshot,
    SessionInspectorSnapshot,
    SmartRouterSnapshot,
    SmartRouterTierSnapshot,
    TuiState,
    TranscriptItem,
    UsageSnapshot,
)


StateListener = Callable[[TuiState], None]
MAX_QUEUE_SIZE = 20


@dataclass(frozen=True)
class QueuedSubmission:
    id: str
    text: str
    kind: QueueItemKind


SYSTEM_RESUME_INTENT_PROMPT = (
    "你负责判断用户输入是针对「未完成任务」的继续指令，还是全新的对话指令。\n"
    "规则：\n"
    "1. 如果用户表达继续/接着/完成刚才那个任务/恢复任务 → 输出 {\"intent\": \"resume_task\"}\n"
    "2. 如果用户在回应任务等待的信息（例如补充允许修改的路径、回答任务提出的问题）→ 输出 {\"intent\": \"provide_input\"}\n"
    "3. 如果用户提出与待办任务无关的新问题或新指令 → 输出 {\"intent\": \"new_chat\"}\n"
    "无法判断时优先选 new_chat。只输出一行 JSON，不要解释。"
)


@dataclass
class ResumableTask:
    """一个可被「继续」自然语言恢复的任务执行器。同一时刻只保留最近一个。"""

    kind: Literal["plan", "team"]
    executor: PlanExecutor | TeamExecutor
    goal: str
    turn_id: str
    user_cancelled: bool = False
    resume_count: int = 0


class SessionController:
    """Own the active turn and expose actions to any UI adapter.

    The controller deliberately has no Textual imports.  ``submit`` can be
    awaited by tests or run as an asyncio task by the TUI, while all event
    handling follows the same reducer path.
    """

    def __init__(
        self,
        agent: ReActAgent,
        settings: Settings,
        manager: ConfigManager,
        on_state_change: StateListener | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.manager = manager
        self.on_state_change = on_state_change
        policy = agent.approval_policy
        memory_snapshot = self._read_memory_snapshot(agent.memory_manager)
        self.state = TuiState(
            ui_language=settings.ui_language,
            inspector=replace(
                TuiState().inspector,
                session=SessionInspectorSnapshot(
                    provider=settings.provider,
                    model=settings.model,
                    status="Idle",
                ),
                memory=memory_snapshot,
                safety=SafetyInspectorSnapshot(
                    hitl_enabled=bool(policy and policy.enabled),
                    session_allow_all=bool(policy and policy.session_allow_all),
                ),
                provider=settings.provider,
                model=settings.model,
                usage=UsageSnapshot(context_window=settings.context_window),
                context_window=settings.context_window,
                hitl_enabled=bool(agent.approval_policy and agent.approval_policy.enabled),
            )
        )
        self.command_service = CommandService(CommandContext(agent, settings, manager), log_sink=self._append_system)
        self._counter = itertools.count(1)
        self._queue_counter = itertools.count(1)
        self._queue: deque[QueuedSubmission] = deque()
        self._queue_worker: asyncio.Task | None = None
        self._shutting_down = False
        self._active_task: asyncio.Task | None = None
        self._active_turn_id = ""
        self._approval_future: asyncio.Future[ApprovalDecision] | None = None
        self._review_future: asyncio.Future[ReviewDecision] | None = None
        self._ask_future: asyncio.Future[dict[str, str] | None] | None = None
        self._team_executor: TeamExecutor | None = None
        self._resumable: ResumableTask | None = None
        self._confirmation: ConfirmationRequest | None = None
        self._confirmation_future: asyncio.Future[bool] | None = None
        mcp_manager = getattr(agent, "mcp_manager", None)
        if mcp_manager is not None:
            mcp_manager.add_listener(self._on_mcp_event)
        if agent.approval_policy is not None:
            agent.approval_policy.requester = self._request_approval
        agent.ask_requester = self._request_ask
        # SmartRouter 防降级上下文（600s 内最多降一档），与 inline 主循环一致
        self._router_prev_tier: str | None = None
        self._router_prev_ts: float | None = None
        # SmartRouter 反馈采集（phase-03 步骤 B）
        self._feedback = FeedbackRecorder(
            session=str(getattr(manager, "project_dir", "") or "")
        )
        # SmartRouter 校准（phase-03 步骤 C）：启动时聚合 feedback.log 并落盘
        from xg.adaptive.calibrate import recalibrate
        self._calibration = recalibrate()
        # SmartRouter 自学习/稳定/ML（phase-04 A1/A2 + phase-05 B2）：挂到 agent 上，
        # 与 inline 主循环自洽；learned_rules 供路由局部微调、hysteresis 供稳定层、
        # ml 供 status 显示与精判。产物存在可用则参与精判，否则静默回落。
        from xg.adaptive.learned_rules import re_learn
        from xg.router.ml_router import MLRouter
        from xg.router.postprocess import Hysteresis
        from xg.router.semantic import load_semantic_encoder
        self.agent._smart_calibration = self._calibration
        self.agent._smart_learned = re_learn()
        self.agent._smart_hysteresis = Hysteresis()
        self.agent._smart_ml = MLRouter(semantic=load_semantic_encoder())
        self._sync_smart_router_snapshot()

    @staticmethod
    def _read_memory_snapshot(memory, *, last_operation: str = "") -> MemoryInspectorSnapshot:
        if memory is None:
            return MemoryInspectorSnapshot(
                store_available=False,
                last_operation=last_operation or "unavailable",
            )
        try:
            project = memory.project_loader.load()
            sources = {section.source for section in project.sections}
            warnings = len(memory.warnings())
            try:
                count = memory.count()
            except Exception:
                count = 0
            return MemoryInspectorSnapshot(
                project_root=str(memory.project_root),
                xg_loaded="XG.md" in sources,
                xg_local_loaded="XG.local.md" in sources,
                warning_count=warnings,
                memory_count=count,
                store_available=memory.store is not None,
                last_operation=last_operation or "ready",
            )
        except Exception:
            return MemoryInspectorSnapshot(
                project_root=str(getattr(memory, "project_root", "")),
                store_available=False,
                last_operation=last_operation or "unavailable",
            )

    def set_inspector_view(self, view: InspectorView) -> bool:
        """Select an Inspector view without creating a transcript event."""
        if view not in ("session", "plan", "memory", "safety"):
            return False
        if self.state.inspector.active_view == view:
            return True
        self._set_state(replace(
            self.state,
            inspector=replace(self.state.inspector, active_view=view),
        ))
        return True

    def cycle_inspector_view(self, step: int = 1) -> None:
        views: tuple[InspectorView, ...] = ("session", "plan", "memory", "safety")
        try:
            index = views.index(self.state.inspector.active_view)
        except ValueError:
            index = 0
        self.set_inspector_view(views[(index + step) % len(views)])

    def snapshot(self) -> TuiState:
        return self.state

    @property
    def busy(self) -> bool:
        active = self._active_task is not None and not self._active_task.done()
        worker = self._queue_worker is not None and not self._queue_worker.done()
        return active or worker or self._confirmation is not None

    def _publish(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(self.state)

    def _sync_smart_router_snapshot(self, active_tier: str = "") -> None:
        """按当前开关与档位配置重建 Header 快照（phase-02 步骤 C）。

        off 态写入空快照（Header 回到单行渲染）；已同步为 off 时不重复写。
        """
        current = self.state.inspector.smart_router
        if not self.settings.smart_router_enabled:
            if current.enabled or current.tiers:
                self._set_state(set_smart_router_snapshot(self.state, SmartRouterSnapshot()))
            return
        tiers_cfg = self.manager.smart_router_config().get("tiers") or {}
        entries: list[SmartRouterTierSnapshot] = []
        for idx, name in enumerate(TIER_NAMES):
            target = resolve_tier(idx, self.settings.provider, self.settings.model, tiers_cfg, self.manager)
            entries.append(SmartRouterTierSnapshot(
                tier=name,
                provider=target.provider,
                model=target.model,
                is_active=(name == active_tier),
                configured=target.configured,
            ))
        # 路由可能已切换模型：同步 Header 主行与上下文窗口，避免显示滞后
        state = replace(
            self.state,
            inspector=replace(
                self.state.inspector,
                provider=self.settings.provider,
                model=self.settings.model,
                usage=replace(self.state.inspector.usage, context_window=self.settings.context_window),
                context_window=self.settings.context_window,
            ),
        )
        self._set_state(set_smart_router_snapshot(
            state,
            SmartRouterSnapshot(enabled=True, tiers=tuple(entries), active_tier=active_tier),
        ))

    def _route_user_turn(self, text: str) -> None:
        """SmartRouter：普通轮在执行前路由并切换模型（与 inline 主循环行为一致）。

        只在开关开启时执行；切换失败保持上一档防降级上下文并仍按结果档展示快照。
        同时接入反馈信号采集（phase-03 步骤 B：clarify / cmd_retry / short_high_tier）。
        """
        if not self.settings.smart_router_enabled:
            return
        # 延迟导入避免环：xg.cli.app → xg.tui.app → 本模块
        from xg.cli.app import _attach_model

        tiers_cfg = self.manager.smart_router_config().get("tiers") or {}
        now = time.time()
        result = route_turn(
            text,
            prev_tier=self._router_prev_tier, prev_ts=self._router_prev_ts, ts=now,
            fallback_provider=self.settings.provider, fallback_model=self.settings.model,
            tiers_config=tiers_cfg, manager=self.manager, calibration=self._calibration,
            learned_rules=getattr(self.agent, "_smart_learned", None),
            ml_router=getattr(self.agent, "_smart_ml", None),
        )
        err: str | None = None
        # 先按上一轮档位采集 clarify/cmd_retry/short_high_tier，并立即落盘
        # （与 inline _route_user_turn 保持一致，不依赖本轮切换是否成功）
        capture_turn_signals(
            self._feedback, text, getattr(result, "features", None) or {},
            self._router_prev_tier, result.tier, TIER_NAMES,
        )
        self._feedback.flush()
        # 再执行模型切换；切换失败仅影响"当轮是否真的用了该档 + 快照展示"，
        # 不阻断信号采集，也不阻断防降级上下文 prev_tier 的推进
        if (result.provider, result.model) != (self.settings.provider, self.settings.model):
            err = _attach_model(self.settings, self.manager, self.agent, result.provider, result.model)
        if err is None:
            self._router_prev_tier, self._router_prev_ts = result.tier, now
        self._sync_smart_router_snapshot(result.tier)

    def _set_state(self, state: TuiState) -> None:
        self.state = state
        self._publish()

    async def _on_mcp_event(self, event) -> None:
        """Expose MCP lifecycle/resource events without coupling MCP to Textual."""
        if self._shutting_down:
            return
        if event.kind == "mcp_server_unavailable":
            self._append_item(TranscriptItem(
                id=f"mcp-{event.server}-{len(self.state.transcript)}",
                kind="error",
                text=f"MCP Server {event.server} 不可用：{event.text}",
            ))
        elif event.kind == "mcp_resource_read":
            self._append_item(TranscriptItem(
                id=f"mcp-resource-{len(self.state.transcript)}",
                kind="context",
                text=f"已读取 MCP resource：{event.server}:{event.text}",
                collapsible=True,
                collapsed=True,
            ))
        elif event.kind == "mcp_server_ready":
            self._set_state(replace(
                self.state,
                notification=(
                    f"MCP Server {event.server} 已连接"
                    f"（{event.tool_count or 0} tools / {event.resource_count or 0} resources）"
                ),
                notification_level="info",
            ))

    def _begin_turn(self) -> str | None:
        # The queue worker may be alive while it is starting the next turn;
        # only an active turn itself prevents a new turn from beginning.
        if self._active_task is not None and not self._active_task.done():
            self._set_state(replace(self.state, notification="当前任务仍在运行，请先取消", notification_level="warning"))
            return None
        turn_id = f"turn-{next(self._counter)}"
        self._active_turn_id = turn_id
        self._set_state(replace(self.state, phase="running", active_turn_id=turn_id, notification=""))
        return turn_id

    @staticmethod
    def _submission_kind(text: str) -> QueueItemKind:
        lowered = text.lower()
        if lowered.startswith("/plan"):
            return "plan"
        if lowered.startswith("/team"):
            return "plan"
        if lowered.startswith("/"):
            return "command"
        return "task"

    def _publish_queue(self, *, notification: str | None = None, level: str = "info") -> None:
        queue_items = [
            QueueItem(id=item.id, text=item.text, kind=item.kind)
            for item in self._queue
        ]
        updates = {"queue": queue_items}
        if notification is not None:
            updates.update(notification=notification, notification_level=level)
        self._set_state(replace(self.state, **updates))

    def _enqueue(self, text: str) -> bool:
        if len(self._queue) >= MAX_QUEUE_SIZE:
            self._set_state(replace(
                self.state,
                notification=f"任务队列已满（最多 {MAX_QUEUE_SIZE} 项），请稍后再试",
                notification_level="warning",
            ))
            return False
        item = QueuedSubmission(
            id=f"queue-{next(self._queue_counter)}",
            text=text,
            kind=self._submission_kind(text),
        )
        self._queue.append(item)
        self._publish_queue(notification=f"已加入队列 #{item.id.removeprefix('queue-')}")
        return True

    def _ensure_queue_worker(self) -> None:
        if self._shutting_down or not self._queue:
            return
        if self._queue_worker is None or self._queue_worker.done():
            self._queue_worker = asyncio.create_task(self._drain_queue())

    async def _drain_queue(self) -> None:
        current = asyncio.current_task()
        try:
            while self._queue and not self._shutting_down:
                submission = self._queue.popleft()
                self._publish_queue(notification=f"开始执行队列 #{submission.id.removeprefix('queue-')}")
                turn_task = asyncio.create_task(self._execute_one(submission.text))
                try:
                    await turn_task
                    if self._confirmation_future is not None:
                        await self._confirmation_future
                except asyncio.CancelledError:
                    if self._shutting_down:
                        break
                except Exception as exc:
                    self._set_state(replace(
                        self.state,
                        notification=f"队列任务失败：{exc}",
                        notification_level="error",
                    ))
        finally:
            if self._queue_worker is current:
                self._queue_worker = None
            if self._shutting_down:
                self._queue.clear()
                self._publish_queue()

    def completion_registry(self):
        """Build a runtime provider registry for dynamic command completion.

        Each provider reads only local, in-memory state from the live agent —
        never LLM calls, MCP starts/restarts, network I/O or tool execution.
        Polling happens on every keys/refresh; the engine caps and degrades
        gracefully, and the Agent's data is already kept in memory.
        """
        from xg.cli.completion import (
            CompletionCandidate,
            CompletionContext,
            CompletionProviderRegistry,
            path_completion_candidates,
        )

        reg = CompletionProviderRegistry()
        mgr = self.manager

        def _providers():
            return mgr.provider_names()

        def _models():
            # A provider is not advertised with a full model list, so only the
            # default model (plus any configured active model) is offered.
            providers = mgr.provider_names()
            seen = set(mgr.active().model)
            for name in providers:
                p = mgr.resolve_provider(name)
                if p is not None:
                    seen.add(p.default_model)
            return sorted(seen)

        reg.register(
            "provider",
            lambda ctx: [
                CompletionCandidate(name, name, detail="provider", kind="value")
                for name in _providers()
            ],
        )
        reg.register(
            "model",
            lambda ctx: [
                CompletionCandidate(m, m, detail="model", kind="value") for m in _models()
            ],
        )

        mcp = getattr(self.agent, "mcp_manager", None)
        reg.register(
            "mcp",
            lambda ctx: [
                CompletionCandidate(
                    snap.name, snap.name, detail="MCP server", kind="value"
                )
                for snap in (mcp.snapshots() if mcp is not None else [])
            ],
        )

        skills = getattr(self.agent, "skill_registry", None)
        reg.register(
            "skill",
            lambda ctx: [
                CompletionCandidate(info.name, info.name, detail="skill", kind="value")
                for info in (
                    skills.list() if skills is not None and skills.config.enabled else ()
                )
                if info.enabled
            ],
        )

        memory = getattr(self.agent, "memory_manager", None)
        reg.register(
            "memory_id",
            lambda ctx: [
                CompletionCandidate(str(entry.id), str(entry.id), detail="记忆", kind="value")
                for entry in (memory.list(20) if memory is not None else [])
            ],
        )

        executor = self._team_executor
        project_root = getattr(self.agent.memory_manager, "project_root", None)

        def _write_claim_patterns():
            if executor is None or executor.plan is None:
                return []
            patterns: set[str] = set()
            for task in executor.plan.tasks:
                if task.status != "needs_input":
                    continue
                for claim in getattr(task, "pending_repair_scope", []):
                    if claim.access == "write":
                        patterns.add(ResourceClaim(claim.pattern, "write").normalized())
            return sorted(patterns)

        def _team_team_scope_candidates(ctx: CompletionContext):
            declared: list[CompletionCandidate] = []
            if executor is not None and executor.plan is not None:
                for task in executor.plan.tasks:
                    if task.status != "needs_input":
                        continue
                    for claim in getattr(task, "pending_repair_scope", []):
                        path = getattr(claim, "pattern", "") or ""
                        if path:
                            declared.append(
                                CompletionCandidate(path, path, detail="已声明范围", kind="scope")
                            )
            if project_root is None:
                return declared
            path_cands = path_completion_candidates(
                ctx.raw,
                ctx.cursor_position,
                project_root,
                allow_patterns=_write_claim_patterns(),
            )
            merged = list(declared)
            seen = {c.insert_text for c in merged}
            for cand in path_cands:
                if cand.insert_text not in seen:
                    merged.append(cand)
                    seen.add(cand.insert_text)
            return merged

        reg.register("team_scope", lambda ctx: _team_team_scope_candidates(ctx))
        reg.register(
            "team_task",
            lambda ctx: [
                CompletionCandidate(task.id, task.id, detail=task.title, kind="value")
                for task in (
                    executor.plan.tasks if executor is not None and executor.plan is not None
                    else []
                )
                if task.status == "needs_input"
            ],
        )
        return reg

    async def submit(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        lowered = text.lower()
        if parse_help_command(text) is not None:
            result = await self.execute_command(text)
            if result.message:
                self._append_help(result.message)
            return result.ok
        if lowered in ("/cancel", "/c"):
            await self.cancel()
            return True
        if lowered in ("/exit", "/quit"):
            self._shutting_down = True
            await self.cancel()
            self._queue.clear()
            self._publish_queue(notification="正在退出，已清理等待队列")
            return True
        if self.state.phase == "awaiting_plan_review" and not self._is_language_command(text):
            self._set_state(replace(
                self.state,
                notification="当前处于计划审阅，请按 Enter 执行、r 重规划或 Esc 取消",
                notification_level="info",
            ))
            return False
        if self.state.phase == "awaiting_team_input":
            if lowered.startswith("/team resume"):
                return await self._resume_team_command(text)
            self._set_state(replace(
                self.state,
                notification="当前 Team 任务等待输入，请使用 /team resume <任务ID> --write-scope <范围>，或 /cancel",
                notification_level="info",
            ))
            return False
        if self.busy and (self._is_readonly_mcp_command(text) or self._is_language_command(text)):
            result = await self.execute_command(text)
            if result.message:
                self._append_system(result.message)
            return result.ok
        if self.busy or self._queue:
            return self._enqueue(text)
        try:
            return await self._execute_one(text)
        finally:
            self._ensure_queue_worker()

    async def _execute_one(self, text: str) -> bool:
        lowered = text.lower()
        if text.startswith("/") and not lowered.startswith(("/plan", "/team", "/ask")):
            current = asyncio.current_task()
            self._active_task = current
            try:
                result = await self.execute_command(text)
                if result.message and not result.open_modal:
                    self._append_system(result.message)
                # /smartRouter、/model 等命令可能改变开关或档位配置，同步 Header 快照
                self._sync_smart_router_snapshot()
                return True
            finally:
                if self._active_task is current:
                    self._active_task = None

        # V3：存在未完成（且未被用户取消）的任务时，先尝试自然语言断点续跑
        if await self._maybe_resume(text):
            return True

        turn_id = self._begin_turn()
        if turn_id is None:
            return False
        self._append_item(TranscriptItem(id=f"user-{len(self.state.transcript)}", kind="user", text=text, turn_id=turn_id))
        is_plan = lowered.startswith("/plan")
        is_team = lowered.startswith("/team")
        is_ask = lowered.startswith("/ask")
        goal = text[5:].strip() if (is_plan or is_team or is_ask) else ""
        if (is_plan or is_team or is_ask) and not goal:
            self._append_system(f"用法: {'/team' if is_team else '/plan' if is_plan else '/ask'} <任务描述>")
            return True
        if is_plan or is_team:
            progress_text = "正在生成执行计划"
        else:
            progress_text = "正在准备响应"
        self._append_item(TranscriptItem(
            id=f"progress-{turn_id}",
            kind="progress",
            progress_kind="plan" if is_plan else "response",
            text=progress_text,
            turn_id=turn_id,
            trace_id=turn_id,
            status="running",
        ))
        current = asyncio.current_task()
        self._active_task = current
        try:
            if is_plan:
                await self._run_plan(goal, turn_id)
            elif is_team:
                await self._run_team(goal, turn_id)
            elif is_ask:
                await self._run_ask(goal, turn_id)
            else:
                # 普通轮执行前路由（与 inline _run_loop_body 一致；/plan、/team 不路由）
                self._route_user_turn(text)
                await self._run_agent(text, turn_id)
        except asyncio.CancelledError:
            cancelled = finalize_trace(self.state, turn_id, status="cancelled")
            self._set_state(replace(cancelled, phase="idle", pending_approval=None, pending_plan=None, notification="当前任务已取消"))
            raise
        finally:
            self._remove_progress(turn_id)
            if self._active_task is current:
                self._active_task = None
            self._approval_future = None
            self._ask_future = None
            self._review_future = None
            if self.state.active_turn_id == turn_id and self.state.phase == "running":
                self._set_state(replace(self.state, phase="idle", pending_approval=None, pending_plan=None))
        return True

    async def _run_agent(self, text: str, turn_id: str) -> None:
        async for event in self.agent.run(text):
            self._set_state(reduce_agent_event(self.state, event, turn_id))

    async def _run_ask(self, goal: str, turn_id: str) -> None:
        """/ask <任务>：强制模型先向用户收齐关键信息再动手。

        通过临时注入一条 user 指令实现——复用同一个 ReAct agent，但本轮开头先
        要求模型调用 ask_user。指令作为正常 assistant 上下文保留，不回写持久配置。
        """
        asked = (
            "这是用户通过 /ask 发起的任务，要求你执行前先向用户确认关键信息。\n"
            "规则：开始执行任何工具前，先调用 ask_user 向你提问，用问题+选项收齐"
            "范围、约束、取舍等关键不确定点；用户回答后再依次执行。除非确实没有需要"
            "澄清的地方，否则不要直接跳过。\n"
            f"任务如下：\n{goal}"
        )
        async for event in self.agent.run(asked):
            self._set_state(reduce_agent_event(self.state, event, turn_id))

    async def _run_plan(self, goal: str, turn_id: str) -> None:
        executor = PlanExecutor(
            llm=self.agent.llm,
            tools=self.agent.tools,
            settings=self.settings,
            reviewer=self._review_plan,
            approval_policy=self.agent.approval_policy,
            audit=self.agent.audit,
            memory_manager=self.agent.memory_manager,
            mcp_manager=getattr(self.agent, "mcp_manager", None),
            ask_requester=self.agent.ask_requester,
        )
        self._resumable = ResumableTask(kind="plan", executor=executor, goal=goal, turn_id=turn_id)
        async for event in executor.run(goal):
            self._set_state(reduce_plan_event(self.state, event, turn_id))
            self._finalize_task_registry(event, turn_id)

    async def _run_team(self, goal: str, turn_id: str) -> None:
        executor = TeamExecutor(
            llm=self.agent.llm,
            tools=self.agent.tools,
            settings=self.settings,
            reviewer=self._review_plan,
            approval_policy=self.agent.approval_policy,
            audit=self.agent.audit,
            memory_manager=self.agent.memory_manager,
            mcp_manager=getattr(self.agent, "mcp_manager", None),
            project_root=getattr(self.agent.memory_manager, "project_root", None),
            ask_requester=self.agent.ask_requester,
        )
        self._team_executor = executor
        self._resumable = ResumableTask(kind="team", executor=executor, goal=goal, turn_id=turn_id)
        async for event in executor.run(goal):
            self._set_state(reduce_team_event(self.state, event, turn_id))
            self._finalize_task_registry(event, turn_id)

    async def _resume_team_command(self, text: str) -> bool:
        executor = self._team_executor
        if executor is None:
            self._set_state(replace(self.state, notification="没有可恢复的 Team 任务", notification_level="warning"))
            return False
        try:
            parts = shlex.split(text)
        except ValueError as exc:
            self._set_state(replace(self.state, notification=f"恢复命令格式错误：{exc}", notification_level="error"))
            return False
        if len(parts) < 5 or parts[0].lower() != "/team" or parts[1].lower() != "resume":
            self._set_state(replace(self.state, notification="用法：/team resume <任务ID> --write-scope <项目内路径模式>", notification_level="info"))
            return False
        task_id = parts[2]
        claims: list[ResourceClaim] = []
        index = 3
        while index < len(parts):
            if parts[index] != "--write-scope" or index + 1 >= len(parts):
                self._set_state(replace(self.state, notification="用法：/team resume <任务ID> --write-scope <项目内路径模式>", notification_level="info"))
                return False
            claims.append(ResourceClaim(parts[index + 1], "write"))
            index += 2
        current = asyncio.current_task()
        self._active_task = current
        self._set_state(replace(self.state, phase="running", notification="正在按确认范围恢复 Team 任务"))
        try:
            async for event in executor.resume_task_with_repair_scope(task_id, claims):
                self._set_state(reduce_team_event(self.state, event, self._active_turn_id))
        finally:
            if self._active_task is current:
                self._active_task = None
            if self.state.phase == "running":
                self._set_state(replace(self.state, phase="idle"))
        return True

    # ---- V3：任务自然语言断点续跑 ----

    _RESUME_KEYWORDS = ("继续", "接着", "继续执行", "continue", "go on", "做完", "把它完成", "恢复任务")

    async def _maybe_resume(self, text: str) -> bool:
        """存在可恢复任务时尝试路由；返回 True 表示输入已被消费为恢复动作。"""
        task = self._resumable
        if task is None or task.user_cancelled:
            return False
        if task.resume_count >= self.settings.task_max_resumes:
            self._set_state(replace(
                self.state, notification=f"该任务已恢复 {task.resume_count} 次，超过上限，请重新发起任务",
                notification_level="warning",
            ))
            return True
        intent = await self._classify_resume_intent(text, task)
        if intent == "new_chat":
            # 与任务无关的新指令，走主 agent 普通对话
            return False
        if intent == "provide_input":
            return await self._resume_with_scope(task, text)
        return await self._do_resume(task, text)

    async def _classify_resume_intent(self, text: str, task: ResumableTask) -> Literal["resume_task", "new_chat", "provide_input"]:
        """LLM 意图识别；关闭或失败时回退关键词白名单。"""
        if self.settings.resume_intent_llm and task.executor.llm is not None:
            try:
                summary = self._summarize_task(task)
                messages = [
                    Message(role="system", content=SYSTEM_RESUME_INTENT_PROMPT),
                    Message(role="user", content=f"任务：{summary}\n用户输入：{text}\n\n只输出 JSON。"),
                ]
                parts: list[str] = []
                async for event in task.executor.llm.stream_chat(messages, tools=None):
                    if event.kind == "content" and event.text:
                        parts.append(event.text)
                parsed = json.loads("".join(parts))
                kind = parsed.get("intent")
                if kind in ("resume_task", "new_chat", "provide_input"):
                    return kind
            except Exception:
                pass
        return "resume_task" if self._is_resume_keyword(text) else ("provide_input" if self._is_provide_input_keyword(text) else "new_chat")

    @staticmethod
    def _is_resume_keyword(text: str) -> bool:
        lowered = text.lower()
        for kw in ("继续", "接着", "continue", "go on", "做完", "把它完成", "恢复任务", "继续执行"):
            if kw in lowered:
                return True
        return False

    @staticmethod
    def _is_provide_input_keyword(text: str) -> bool:
        lowered = text.lower()
        patterns = ("允许修改", "允许写", "修改范围", "范围", "write-scope", "write scope", "可以改")
        return any(pattern in lowered for pattern in patterns)

    async def _do_resume(self, task: ResumableTask, instruction: str) -> bool:
        """执行断点续跑：追加轮次、跑 executor.resume()。"""
        current = asyncio.current_task()
        turn_id = self._begin_turn()
        if turn_id is None:
            return False
        self._append_item(TranscriptItem(id=f"user-{len(self.state.transcript)}", kind="user", text=instruction, turn_id=turn_id))
        self._append_item(TranscriptItem(
            id=f"progress-{turn_id}", kind="progress",
            progress_kind="plan", text="正在恢复执行", turn_id=turn_id,
            trace_id=turn_id, status="running",
        ))
        task.resume_count += 1
        task.turn_id = turn_id
        self._active_task = current
        try:
            if task.kind == "team":
                async for event in task.executor.resume(instruction):  # type: ignore[union-attr]
                    self._set_state(reduce_team_event(self.state, event, turn_id))
                    self._finalize_task_registry(event, turn_id, task=task)
            else:
                async for event in task.executor.resume(instruction):  # type: ignore[union-attr]
                    self._set_state(reduce_plan_event(self.state, event, turn_id))
                    self._finalize_task_registry(event, turn_id, task=task)
        except asyncio.CancelledError:
            self._set_state(replace(self.state, phase="idle", notification="任务已取消"))
            raise
        finally:
            self._remove_progress(turn_id)
            if self._active_task is current:
                self._active_task = None
            if self.state.active_turn_id == turn_id and self.state.phase == "running":
                self._set_state(replace(self.state, phase="idle"))
        return True

    async def _resume_with_scope(self, task: ResumableTask, text: str) -> bool:
        """needs_input 场景：从用户自然语言里提取写入范围，过安全校验后恢复 Repairer（fail-closed）。"""
        if task.kind != "team":
            self._set_state(replace(self.state, notification="仅 Team 任务需要补充写入范围", notification_level="info"))
            return False
        claims = self._extract_scope_claims(text)
        if not claims:
            self._set_state(replace(
                self.state,
                notification="请回复允许修改的项目内路径，例如「继续，允许修改 xg/auth/」；或使用 /team resume <ID> --write-scope <路径>",
                notification_level="warning",
            ))
            return True
        # 先定位 needs_input 任务
        plan = getattr(task.executor, "_last_plan", None)
        task_id = ""
        if plan is not None:
            for t in plan.tasks:
                if t.status == "needs_input":
                    task_id = t.id
                    break
        if not task_id:
            self._set_state(replace(self.state, notification="当前没有等待写入范围的 Team 任务", notification_level="warning"))
            return True
        current = asyncio.current_task()
        self._active_task = current
        self._set_state(replace(self.state, phase="running", notification="正在校验写入范围并恢复 Team 任务"))
        try:
            async for event in task.executor.resume_task_with_repair_scope(task_id, claims):  # type: ignore[union-attr]
                self._set_state(reduce_team_event(self.state, event, self._active_turn_id))
                # 校验失败会回到 needs_input；校验通过后继续跑剩余批次
                if event.kind in {"team_done", "team_failed", "cancelled"}:
                    self._finalize_task_registry(event, self._active_turn_id, task=task)
        finally:
            if self._active_task is current:
                self._active_task = None
            if self.state.phase == "running":
                self._set_state(replace(self.state, phase="idle"))
        return True

    @staticmethod
    def _extract_scope_claims(text: str) -> list[ResourceClaim]:
        # 提取 `允许修改 X` / `修改范围 X` / `--write-scope X` 后面的项目内路径片段
        claims: list[ResourceClaim] = []
        import re as _re
        for m in _re.finditer(r"(?:允许修改|修改范围|可以改|--write-scope)\s*[:：]?\s*([A-Za-z0-9_./\\*-]+)", text):
            pattern = m.group(1).strip('"\'，。；:：')
            if pattern and " " not in pattern:
                claims.append(ResourceClaim(pattern, "write"))
        return claims

    def _finalize_task_registry(self, event, turn_id: str, *, task: ResumableTask | None = None) -> None:
        """根据终态事件分类维护可恢复任务注册表 + 摘要回填。"""
        kind = getattr(event, "kind", "")
        entry = task or self._resumable
        if entry is None:
            return
        if kind in ("plan_done", "team_done"):
            # done 保留摘要（new_chat 可答），但不再开放恢复
            if entry.turn_id == turn_id:
                self._append_task_summary(entry, terminal="完成")
        elif kind in ("plan_failed", "team_failed"):
            if entry.turn_id == turn_id:
                self._append_task_summary(entry, terminal="失败")
                self._set_state(replace(self.state, notification=f"{event.message}（输入「继续」可从失败处恢复）", notification_level="warning"))
        elif kind == "cancelled":
            # 用户取消会经 cancel() 清除；此处兜底（系统 fail-closed 取消保留）
            if entry.turn_id == turn_id:
                self._resumable = None
        elif kind in ("plan_resume_requested", "team_resume_requested"):
            # 恢复流自身不改变注册表可恢复状态
            pass

    def _summarize_task(self, task: ResumableTask) -> str:
        plan = getattr(task.executor, "_last_plan", None)
        if plan is None:
            return f"目标：{task.goal}"
        lines = [f"类型：{'/team' if task.kind == 'team' else '/plan'}", f"目标：{plan.goal}"]
        for t in plan.tasks:
            status = getattr(t, "status", "?")
            title = getattr(t, "title", getattr(t, "description", ""))
            result = getattr(t, "result", "") or ""
            if result and len(result) > 200:
                result = result[:200] + "…"
            lines.append(f"- {getattr(t, 'id', '?')} [{status}] {title}" + (f"：{result}" if result else ""))
        return "\n".join(lines)

    def _append_task_summary(self, task: ResumableTask, *, terminal: str) -> None:
        """把任务终态摘要回填主 agent 上下文，保证后续普通对话不断裂。"""
        try:
            summary = self._summarize_task(task) + f"\n终态：{terminal}"
        except Exception:  # 摘要构造失败不影响主流程
            return
        try:
            self.agent.context.append(Message(role="assistant", content=f"[任务执行摘要]\n{summary}"))
        except Exception:
            pass

    async def cancel(self) -> bool:
        if self._confirmation is not None:
            await self.confirm_command(False)
            return True
        task = self._active_task
        if task is None or task.done():
            self._set_state(replace(self.state, notification="当前没有运行中的任务", notification_level="info"))
            return False
        if self._approval_future and not self._approval_future.done():
            self._approval_future.set_result(ApprovalDecision(allow=False, reason="user_cancelled"))
        if self._ask_future and not self._ask_future.done():
            self._ask_future.set_result(None)
        if self._review_future and not self._review_future.done():
            self._review_future.set_result(ReviewDecision(action="cancel"))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # V3：用户主动取消任务 → 清除可恢复注册表
        self._resumable = None
        return True

    async def _request_approval(self, tool_name: str, level: str, args: dict) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        self._approval_future = loop.create_future()
        request = ApprovalRequest(tool_name=tool_name, level=level, args=args, turn_id=self._active_turn_id)
        policy = self.agent.approval_policy
        self._set_state(replace(
            self.state,
            phase="awaiting_approval",
            pending_approval=request,
            inspector=replace(
                self.state.inspector,
                safety=replace(
                    self.state.inspector.safety,
                    hitl_enabled=bool(policy and policy.enabled),
                    session_allow_all=bool(policy and policy.session_allow_all),
                    approval_status="waiting",
                    current_tool=tool_name,
                    current_level=level,
                ),
            ),
        ))
        try:
            return await self._approval_future
        finally:
            self._approval_future = None

    async def approve_tool(self, decision: ApprovalDecision) -> None:
        if decision.reason == "user_approved_allow_all" and self.agent.approval_policy:
            self.agent.approval_policy.allow_all()
        policy = self.agent.approval_policy
        self._set_state(replace(
            self.state,
            inspector=replace(
                self.state.inspector,
                safety=replace(
                    self.state.inspector.safety,
                    hitl_enabled=bool(policy and policy.enabled),
                    session_allow_all=bool(policy and policy.session_allow_all),
                ),
            ),
        ))
        future = self._approval_future
        if future is not None and not future.done():
            future.set_result(decision)

    async def _request_ask(self, ask) -> dict[str, str] | None:
        """ask_user 的 requester：暂停等用户在 Ask 面板作答，答案回灌给模型。"""
        loop = asyncio.get_running_loop()
        self._ask_future = loop.create_future()
        self._set_state(replace(
            self.state,
            phase="awaiting_ask",
            pending_ask=ask,
            notification=("请完成下方确认（Esc 跳过）" if ask.prompt else "请完成下方确认"),
            notification_level="info",
        ))
        try:
            return await self._ask_future
        finally:
            self._ask_future = None

    async def submit_ask_answer(
        self,
        answers: dict[str, str] | None,
        *,
        cancelled: bool = False,
        request_id: str = "",
    ) -> None:
        """Ask 面板回调：把用户选择/自定义回灌；Esc 视为跳过（fail-closed）。"""
        current = self.state.pending_ask
        if current is None:
            return
        if request_id and current.id != request_id:
            return
        future = self._ask_future
        self._set_state(replace(
            self.state,
            phase="running",
            pending_ask=None,
            notification="" if cancelled else "已收到你的确认，继续执行",
            notification_level="info",
        ))
        if future is not None and not future.done():
            future.set_result(None if cancelled else answers)

    async def _review_plan(self, plan: Plan) -> ReviewDecision:
        loop = asyncio.get_running_loop()
        self._review_future = loop.create_future()
        self._set_state(replace(self.state, phase="awaiting_plan_review", pending_plan=plan))
        try:
            return await self._review_future
        finally:
            self._review_future = None

    async def review_plan(self, decision: ReviewDecision) -> None:
        future = self._review_future
        if future is not None and not future.done():
            future.set_result(decision)

    def toggle_plan_details(self) -> None:
        """Toggle the latest inline plan card without opening another screen."""
        items = list(self.state.transcript)
        for index in range(len(items) - 1, -1, -1):
            if items[index].kind == "plan":
                items[index] = replace(items[index], collapsed=not items[index].collapsed)
                self._set_state(replace(self.state, transcript=items))
                return

    def toggle_diagram_source(self) -> bool:
        """Toggle Mermaid source for the latest assistant diagram."""
        items = list(self.state.transcript)
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if item.kind == "assistant" and "```mermaid" in item.text.lower():
                items[index] = replace(item, diagram_source_visible=not item.diagram_source_visible)
                self._set_state(replace(self.state, transcript=items))
                return True
        return False

    def toggle_trace_item(self, item_id: str) -> None:
        """Toggle one user-selected thinking/tool trace item."""
        items = list(self.state.transcript)
        for index, item in enumerate(items):
            if item.id == item_id and item.collapsible:
                expanded = item.collapsed
                items[index] = replace(
                    item,
                    collapsed=not expanded,
                    user_collapsed=not expanded,
                )
                self._set_state(replace(self.state, transcript=items))
                return

    def toggle_latest_trace(self) -> None:
        """Toggle the newest collapsible thinking/tool item."""
        for item in reversed(self.state.transcript):
            if item.collapsible:
                self.toggle_trace_item(item.id)
                return

    def toggle_trace_group(self) -> None:
        """Toggle all collapsible items belonging to the newest trace."""
        latest = next((item for item in reversed(self.state.transcript) if item.collapsible), None)
        if latest is None:
            return
        trace_id = latest.trace_id
        members = [item for item in self.state.transcript if item.collapsible and item.trace_id == trace_id]
        # Mixed state is treated as "collapse all"; only an entirely
        # collapsed group expands on the next group toggle.
        expand = all(item.collapsed for item in members)
        member_ids = {item.id for item in members}
        items = [
            replace(item, collapsed=not expand, user_collapsed=not expand)
            if item.id in member_ids else item
            for item in self.state.transcript
        ]
        self._set_state(replace(self.state, transcript=items))

    def toggle_agent_group(self, group_id: str) -> None:
        """Toggle one Team Agent conversation without affecting its siblings."""
        group = self.state.agent_groups.get(group_id)
        if group is None:
            return
        groups = dict(self.state.agent_groups)
        groups[group_id] = replace(
            group,
            collapsed=not group.collapsed,
            user_toggled=True,
        )
        self._set_state(replace(self.state, agent_groups=groups))

    def refresh_diagrams(self) -> None:
        """Republish state so renderables recalculate their terminal layout."""
        self._publish()

    async def execute_command(self, raw: str) -> CommandResult:
        current = asyncio.current_task()
        is_help = parse_help_command(raw) is not None
        is_language = self._is_language_command(raw)
        if self.busy and self._active_task is not current and not self._is_readonly_mcp_command(raw) and not is_help and not is_language:
            return CommandResult(ok=False, message="当前任务正在运行，请通过输入提交命令以加入队列")
        if raw.strip().lower() in ("/cancel", "/c"):
            await self.cancel()
            return CommandResult(ok=True, message="已取消当前任务")
        cleared = raw.strip().lower() == "/clear"
        if cleared:
            self.agent.clear()
            self._resumable = None
        lowered = raw.strip().lower()
        memory = getattr(self.agent, "memory_manager", None)
        if lowered == "/init" and memory is not None:
            try:
                draft = await memory.generate_init_draft(self.agent.llm)
            except Exception as exc:
                return CommandResult(ok=False, message=f"生成 XG.md 失败：{exc}")
            request = ConfirmationRequest(
                "init",
                "写入项目记忆",
                f"已生成 XG.md 草稿，将写入项目根目录的 XG.md：\n\n{draft}",
                draft,
            )
            self._confirmation = request
            self._confirmation_future = asyncio.get_running_loop().create_future()
            self._set_state(replace(self.state, pending_confirmation=request, notification="请输入 y 确认写入 · n 取消"))
            return CommandResult(ok=True, open_modal="init")
        if lowered == "/memory clear" and memory is not None:
            try:
                count = memory.count()
            except Exception as exc:
                return CommandResult(ok=False, message=f"记忆操作失败：{exc}")
            if count == 0:
                return CommandResult(ok=True, message="当前项目没有长期记忆")
            request = ConfirmationRequest(
                "memory_clear",
                "清空长期记忆",
                f"当前项目有 {count} 条长期记忆。\n清空后不可通过 XG 恢复。",
            )
            self._confirmation = request
            self._confirmation_future = asyncio.get_running_loop().create_future()
            self._set_state(replace(self.state, pending_confirmation=request, notification="请输入 clear 确认清空长期记忆"))
            return CommandResult(ok=True, open_modal="memory_clear")
        result = await self.command_service.execute(raw)
        current_usage = self.state.inspector.usage
        if cleared:
            current_usage = UsageSnapshot(context_window=self.settings.context_window)
        elif self.settings.context_window != current_usage.context_window:
            current_usage = replace(
                current_usage,
                context_window=self.settings.context_window,
                window_ratio=(
                    current_usage.estimated_prompt_tokens / self.settings.context_window
                    if self.settings.context_window > 0 else 0.0
                ),
                budget_usage_ratio=(
                    current_usage.estimated_prompt_tokens / self.settings.token_budget
                    if self.settings.token_budget > 0 else 0.0
                ),
            )
        memory_snapshot = self.state.inspector.memory
        if lowered == "/init" or lowered == "/save" or lowered.startswith("/memory"):
            memory_snapshot = self._read_memory_snapshot(memory, last_operation=lowered)
        policy = self.agent.approval_policy
        self._set_state(replace(
            self.state,
            ui_language=self.settings.ui_language,
            transcript=[] if cleared else self.state.transcript,
            inspector=replace(
                self.state.inspector,
                session=replace(
                    self.state.inspector.session,
                    provider=self.settings.provider,
                    model=self.settings.model,
                ),
                memory=memory_snapshot,
                safety=replace(
                    self.state.inspector.safety,
                    hitl_enabled=bool(policy and policy.enabled),
                    session_allow_all=bool(policy and policy.session_allow_all),
                ),
                provider=self.settings.provider,
                model=self.settings.model,
                usage=current_usage,
                context_tokens=0 if cleared else self.state.inspector.context_tokens,
                context_window=self.settings.context_window,
                hitl_enabled=bool(self.agent.approval_policy and self.agent.approval_policy.enabled),
            ),
        ))
        return result

    @staticmethod
    def _is_language_command(raw: str) -> bool:
        parts = raw.strip().lower().split()
        return bool(parts and parts[0] in {"/lang", "/language"})

    @staticmethod
    def _is_readonly_mcp_command(raw: str) -> bool:
        parts = raw.strip().lower().split()
        return bool(
            parts
            and parts[0] == "/mcp"
            and (len(parts) == 1 or parts[1] in {"status", "logs", "resources"})
        )

    async def confirm_command(self, confirmed: bool) -> None:
        request = self._confirmation
        self._confirmation = None
        if request is None:
            return
        message = "已取消操作"
        if confirmed:
            try:
                memory = self.agent.memory_manager
                if request.kind == "init":
                    path = memory.write_init_draft(str(request.payload))
                    message = f"已生成项目记忆：{path.name}"
                elif request.kind == "memory_clear":
                    removed = memory.clear()
                    message = f"已清空 {removed} 条长期记忆"
            except Exception as exc:
                message = f"操作失败：{exc}"
        memory = getattr(self.agent, "memory_manager", None)
        operation = "init" if request.kind == "init" else "clear"
        self._set_state(replace(
            self.state,
            pending_confirmation=None,
            notification=message,
            notification_level="info",
            inspector=replace(
                self.state.inspector,
                memory=self._read_memory_snapshot(
                    memory,
                    last_operation=operation if confirmed else f"{operation}_cancelled",
                ),
            ),
        ))
        self._append_system(message)
        future = self._confirmation_future
        self._confirmation_future = None
        if future is not None and not future.done():
            future.set_result(confirmed)
        self._ensure_queue_worker()

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._confirmation is not None:
            await self.confirm_command(False)
        if self._active_task is not None and not self._active_task.done():
            await self.cancel()
        worker = self._queue_worker
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._queue.clear()
        self._publish_queue()
        mcp_manager = getattr(self.agent, "mcp_manager", None)
        if mcp_manager is not None:
            await mcp_manager.close()
        for service_name in ("web_search", "web_fetch"):
            service = getattr(self.agent, service_name, None)
            if service is not None:
                await service.close()

    async def startup(self) -> None:
        """Start configured MCP servers without blocking TUI construction."""
        mcp_manager = getattr(self.agent, "mcp_manager", None)
        if mcp_manager is None:
            return
        await mcp_manager.ensure_started()
        for error in mcp_manager.config_errors:
            self._append_system(f"MCP 配置提示：{error}")
        snapshots = mcp_manager.snapshots()
        if snapshots:
            ready = sum(item.status == "ready" for item in snapshots)
            self._set_state(replace(
                self.state,
                notification=f"MCP: {ready}/{len(snapshots)} 个 Server 可用",
                notification_level="info" if ready == len(snapshots) else "warning",
            ))

    def _append_item(self, item: TranscriptItem) -> None:
        state = replace(self.state, transcript=[*self.state.transcript, item])
        self._set_state(state)

    def _append_system(self, message: str) -> None:
        if message:
            self._append_item(TranscriptItem(id=f"system-{len(self.state.transcript)}", kind="system", text=message))

    def _append_help(self, message: str) -> None:
        if message:
            self._append_item(TranscriptItem(id=f"help-{len(self.state.transcript)}", kind="help", text=message))

    def _remove_progress(self, turn_id: str) -> None:
        """Remove the local waiting indicator for one active turn."""
        items = [
            item for item in self.state.transcript
            if not (item.kind == "progress" and item.turn_id == turn_id)
        ]
        if len(items) != len(self.state.transcript):
            self._set_state(replace(self.state, transcript=items))
