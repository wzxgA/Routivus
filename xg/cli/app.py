"""CLI 交互循环：prompt_toolkit 输入 + rich 渲染 + 斜杠命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from xg.agent.plan import Plan, PlanEvent, PlanExecutor, PlanTask, ReviewDecision
from xg.agent.react import DEFAULT_SYSTEM_PROMPT, AgentEvent, ReActAgent
from xg.agent.team import TeamEvent, TeamExecutor, TeamPlan, TeamTask
from xg.adaptive.feedback import FeedbackRecorder, text_hash
from xg.adaptive.signals import capture_interrupt, capture_turn_signals
from xg.config.manager import ConfigManager, ProviderNotConfigured, mask_key
from xg.config.mcp import McpConfigManager
from xg.config.settings import Settings, load_settings
from xg.config.web import WebConfigManager
from xg.config.skills import SkillConfigManager
from xg.router import TIER_NAMES, resolve as resolve_tier, route as route_turn
from xg.cli.help import format_command_help, format_help
from xg.cli.path_heal import ensure_on_path as _ensure_on_path
from xg import __version__
from xg.input_history import HistoryConfig, InputHistory, PromptToolkitHistory
from xg.llm.client import LlmClient, LlmError
from xg.llm.factory import create_client
from xg.memory.manager import MemoryManager, MemoryUnavailableError
from xg.mcp.manager import McpManager
from xg.safety.audit import AuditLogger
from xg.safety.guards import guard_tool_call
from xg.safety.hitl import ApprovalDecision, HITLPolicy
from xg.tui.i18n import UiLanguage, normalize_language, translate
from xg.tool.builtin import build_registry
from xg.web.fetch import WebFetchService
from xg.web.search import WebSearchService
from xg.skill.registry import SkillRegistry

console = Console()

def build_agent(
    settings: Settings,
    base_dir=None,
    config_manager: ConfigManager | None = None,
) -> ReActAgent:
    base = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    client = create_client(
        settings.api_base, settings.api_key, settings.model,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    audit = AuditLogger(base / ".xg" / "audit.log")
    guard = lambda name, args: guard_tool_call(base, name, args)  # noqa: E731
    config_manager = config_manager or ConfigManager(project_dir=base / ".xg")
    web_config_manager = WebConfigManager(
        user_dir=config_manager.user_dir, project_root=base, env=config_manager.env
    )
    web_config = web_config_manager.load()
    if not settings.web_enabled:
        web_config = replace(web_config, enabled=False)
    web_search = WebSearchService(web_config, audit=audit) if web_config.enabled else None
    web_fetch = WebFetchService(web_config, audit=audit) if web_config.enabled else None
    skill_config_manager = SkillConfigManager(
        user_dir=config_manager.user_dir, project_root=base, env=config_manager.env
    )
    skill_config = skill_config_manager.load()
    if not settings.skills_enabled:
        skill_config = replace(skill_config, enabled=False)
    skill_registry = SkillRegistry(
        project_root=base,
        config=skill_config,
        config_manager=skill_config_manager,
        audit=audit,
    )
    input_history = InputHistory(
        project_root=base,
        user_dir=config_manager.user_dir,
        config=HistoryConfig(
            enabled=settings.input_history_enabled,
            persist=settings.input_history_persist,
            max_entries=settings.input_history_max_entries,
            max_entry_chars=settings.input_history_max_chars,
            max_file_bytes=settings.input_history_max_bytes,
        ),
    )
    tools = build_registry(
        base_dir=base,
        max_output_chars=settings.max_tool_output_chars,
        guard=guard,
        audit=audit,
        web_config=web_config,
        web_search=web_search,
        web_fetch=web_fetch,
        skill_registry=skill_registry,
        ask_user_enabled=settings.ask_user_enabled,
    )
    hitl = HITLPolicy(enabled=settings.hitl)
    memory_manager = MemoryManager(
        base,
        project_memory_max_chars=settings.project_memory_max_chars,
        memory_prompt_max_chars=settings.memory_prompt_max_chars,
    )
    mcp_config = McpConfigManager(
        user_dir=config_manager.user_dir,
        project_root=base,
        env=config_manager.env,
        defaults={
            "startup_timeout": settings.mcp_startup_timeout,
            "request_timeout": settings.mcp_request_timeout,
            "shutdown_timeout": settings.mcp_shutdown_timeout,
            "max_output_chars": settings.max_tool_output_chars,
            "max_tools": settings.mcp_max_tools,
            "max_resources": settings.mcp_max_resources,
            "max_message_bytes": settings.mcp_max_message_bytes,
            "resource_max_chars": settings.mcp_resource_max_chars,
            "log_lines": settings.mcp_log_lines,
        },
    )
    mcp_manager = McpManager(
        tools,
        mcp_config,
        approval_policy=hitl,
        audit=audit,
        enabled=settings.mcp_enabled,
        max_servers=settings.mcp_max_servers,
        resource_total_chars=settings.mcp_resource_total_chars,
    )
    system_prompt = DEFAULT_SYSTEM_PROMPT
    skill_index = skill_registry.index_text()
    if skill_index:
        system_prompt += "\n\n" + skill_index
    agent = ReActAgent(
        llm=client,
        tools=tools,
        settings=settings,
        system_prompt=system_prompt,
        approval_policy=hitl,
        audit=audit,
        memory_manager=memory_manager,
        mcp_manager=mcp_manager,
    )
    agent.web_config = web_config
    agent.web_config_manager = web_config_manager
    agent.web_search = web_search
    agent.web_fetch = web_fetch
    agent.skill_registry = skill_registry
    agent.skill_config_manager = skill_config_manager
    agent.input_history = input_history
    return agent


class ApprovalUI:
    """HITL 审批交互：绑定 Live 以暂停流式渲染，读取用户决策。"""

    def __init__(self, session: PromptSession[str], label: str = "plan", language: UiLanguage = "zh") -> None:
        self.session = session
        self.label = label
        self.language = normalize_language(language)
        self._live: Live | None = None

    def bind_live(self, live: Live) -> None:
        self._live = live

    def unbind_live(self) -> None:
        """解绑 Live（进入 /plan 等无 Live 渲染的流程前调用，避免误停旧 Live）。"""
        self._live = None

    async def __call__(self, tool_name: str, level: str, args: dict) -> ApprovalDecision:
        if self._live is not None:
            self._live.stop()
        console.print()
        console.print(Panel(
            Text(f"{translate(self.language, 'ui.approval.title')}: {tool_name}\n{translate(self.language, 'ui.approval.level', level=level)}\nargs: {json.dumps(args, ensure_ascii=False)}"),
            style="yellow",
        ))
        decision = await self._read_choice(tool_name)
        if self._live is not None:
            self._live.start()
        return decision

    async def _read_choice(self, tool_name: str) -> ApprovalDecision:
        while True:
            answer = await self.session.prompt_async(
                HTML(f"<ansiyellow>[HITL] {'Approve (Enter) allow all (a) reject (r) skip (s) edit (e)' if self.language == 'en' else '批准(Enter) 全部放行(a) 拒绝(r) 跳过(s) 改参(e)'} ></ansiyellow> ")
            )
            key = answer.strip().lower()
            if key in ("", "y", "yes", "approve"):
                return ApprovalDecision(allow=True, reason="user_approved")
            if key == "a":
                console.print(Text("All subsequent operations are allowed for this session." if self.language == "en" else "本会话后续操作全部放行。", style="dim"))
                return ApprovalDecision(allow=True, reason="user_approved_allow_all")
            if key in ("r", "n", "no", "deny"):
                return ApprovalDecision(allow=False, reason="user_rejected")
            if key == "s":
                return ApprovalDecision(allow=False, reason="user_skipped")
            if key == "e":
                raw = await self.session.prompt_async(HTML(f"<ansicyan>{'New parameters JSON' if self.language == 'en' else '新参数 JSON'} ></ansicyan> "))
                try:
                    new_args = json.loads(raw.strip())
                except json.JSONDecodeError:
                    console.print(Text("Invalid JSON; please try again." if self.language == "en" else "JSON 解析失败，请重新输入。", style="red"))
                    continue
                return ApprovalDecision(allow=True, args=new_args, reason="user_modified")
            console.print(Text("Invalid input: Enter/a/r/s/e" if self.language == "en" else "无效输入：Enter/a/r/s/e", style="yellow"))


async def handle_turn(
    agent: ReActAgent,
    user_input: str,
    approval_ui: ApprovalUI | None = None,
    *,
    language: UiLanguage = "zh",
) -> None:
    """执行一轮 ReAct 循环并渲染事件流。"""
    language = normalize_language(language if approval_ui is None else approval_ui.language)
    buffer = Text()
    with Live(console=console, vertical_overflow="visible", refresh_per_second=10) as live:
        live.update(Text(""))
        if approval_ui is not None:
            approval_ui.bind_live(live)
        async for event in agent.run(user_input):
            if event.kind == "content":
                buffer.append(event.text)
                live.update(Markdown(buffer.plain))
            elif event.kind == "tool_call" and event.tool_call:
                live.update(Text(""))
                console.print(
                    Text(f"→ {event.tool_call.name}({event.tool_call.arguments})", style="dim cyan")
                )
                live.update(Text(""))
            elif event.kind == "approval" and event.tool_call:
                live.update(Text(""))
                style = {
                    "approved": "green",
                    "modified": "yellow",
                    "rejected": "red",
                }.get(event.text, "yellow")
                console.print(
                    Text(f"  {event.text}: {event.tool_call.name}({event.tool_call.arguments})", style=style)
                )
                live.update(Text(""))
            elif event.kind == "tool_result" and event.tool_result:
                live.update(Text(""))
                style = "green" if event.tool_result.ok else "red"
                preview = (event.tool_result.output or event.tool_result.error).strip()
                if len(preview) > 300:
                    preview = preview[:300] + " ..."
                console.print(Text(f"  {'OK' if event.tool_result.ok else 'FAIL'}: {preview}", style=style))
                live.update(Text(""))
            elif event.kind == "retrying":
                live.update(Text(""))
                console.print(Text(
                    translate(language, "ui.cli.retry", current=event.retry_attempts,
                              total=event.retry_max_attempts, seconds=event.retry_delay or 0),
                    style="yellow",
                ))
                live.update(Text(""))
            elif event.kind == "context_compacted":
                live.update(Text(""))
                console.print(Text(event.text, style="dim cyan"))
                live.update(Text(""))
            elif event.kind == "context_warning":
                live.update(Text(""))
                console.print(Text(translate(language, "ui.cli.context_warning", text=event.text), style="yellow"))
                live.update(Text(""))
            elif event.kind in ("step_limit", "budget_exceeded", "context_overflow", "error"):
                live.update(Text(""))
                if event.kind == "step_limit":
                    msg = f"{translate(language, 'ui.error.step_limit')}"
                elif event.kind in ("budget_exceeded", "context_overflow"):
                    msg = event.text or translate(language, "ui.error.default")
                else:
                    msg = translate(language, "ui.cli.request_failed", error=event.text)
                console.print(Panel(Text(msg), style="yellow"))
                return
    console.print(Text(""))


class PlanReviewUI:
    """计划审阅交互：Enter 执行 / d 展开详情 / r 补充重规划 / ESC（或 c）取消。"""

    def __init__(self, session: PromptSession[str], label: str = "plan", language: UiLanguage = "zh") -> None:
        self.session = session
        self.label = label
        self.language = normalize_language(language)

    async def __call__(self, plan: Plan) -> ReviewDecision:
        # 面板已由 plan_generated 事件渲染（含 warnings），这里只读决策，不重复打印
        while True:
            answer = (await self.session.prompt_async(
                HTML(f"<ansiyellow>[{self.label}] {'Enter execute / d details / r replan / ESC cancel' if self.language == 'en' else 'Enter 执行 / d 详情 / r 重规划 / ESC 取消'} ></ansiyellow> "),
                key_bindings=_escape_cancel_bindings(),
            )).strip().lower()
            if answer == "":
                return ReviewDecision(action="execute")
            if answer == "d":
                _print_plan_details(plan, language=self.language)
                continue
            if answer == "r":
                feedback = await self.session.prompt_async(
                    HTML(f"<ansicyan>[{self.label}] {'Additional requirements (empty input returns without replanning)' if self.language == 'en' else '补充要求（空行返回不重规划）'} ></ansicyan> ")
                )
                feedback = feedback.strip()
                if not feedback:
                    continue
                return ReviewDecision(action="replan", feedback=feedback)
            if answer in ("c", "q", "esc"):
                return ReviewDecision(action="cancel")
            console.print(Text("Invalid input: Enter execute / d details / r replan / ESC cancel" if self.language == "en" else "无效输入：Enter 执行 / d 详情 / r 重规划 / ESC 取消", style="yellow"))


class AskUI:
    """inline 交互询问：展示问题+选项，用户选择或输入自定义；Esc/空跳过（fail-closed）。"""

    def __init__(self, session: PromptSession[str], language: UiLanguage = "zh") -> None:
        self.session = session
        self.language = normalize_language(language)

    async def __call__(self, ask) -> dict[str, str] | None:
        answers: dict[str, str] = {}
        if ask.prompt:
            console.print(Text(ask.prompt, style="bold cyan"))
        for field in ask.fields:
            console.print(Text(f"[{field.key}] {field.question}", style="cyan"))
            options = list(field.options)
            for index, option in enumerate(options, start=1):
                console.print(Text(f"  {index}. {option.label}", style="dim"))
            while True:
                answer = (await self.session.prompt_async(
                    HTML(f"<ansicyan>{'Choose a number or enter custom text (empty=skip, Esc=cancel)' if self.language == 'en' else '选择数字或输入自定义（空=跳过，Esc=取消）'}></ansicyan> ")
                )).strip()
                if answer.lower() in {"c", "q", "esc", "escape"}:
                    return None
                if not answer:
                    if field.required:
                        console.print(Text("This field is required; choose an option or enter an answer." if self.language == "en" else "此项必答，请选择选项或输入回答。", style="yellow"))
                        continue
                    break
                try:
                    selected = int(answer)
                except ValueError:
                    selected = -1
                if 1 <= selected <= len(options):
                    option = options[selected - 1]
                    answers[field.key] = option.value or option.label
                    break
                if not field.allow_custom:
                    console.print(Text("This question only accepts an option number." if self.language == "en" else "此题只能输入选项序号。", style="yellow"))
                    continue
                answers[field.key] = answer
                break
        return answers


def _escape_cancel_bindings() -> KeyBindings:
    """ESC 直接提交为取消（保留 emacs 元前缀以外的行为）。"""
    kb = KeyBindings()

    @kb.add("escape")
    def _cancel(event) -> None:
        event.app.current_buffer.text = "c"
        event.app.current_buffer.validate_and_handle()

    return kb


def _print_plan_panel(plan: Plan, note: str = "", *, language: UiLanguage = "zh") -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", justify="right", no_wrap=True)
    table.add_column()
    for i, batch in enumerate(plan.batches, 1):
        table.add_row(translate(language, "ui.plan.round_label", number=i), Text(", ".join(batch), style="bold"))
        for tid in batch:
            t = plan.task_by_id(tid)
            assert t is not None
            deps = translate(language, "ui.plan.dependency", dependencies=", ".join(t.deps)) if t.deps else ""
            table.add_row("", f"{t.id}  {t.title}{deps}")
    lines = [table]
    if note:
        lines.append(Text(translate(language, "ui.plan.hint", text=note), style="yellow"))
    console.print(Panel(*lines, title=f"{translate(language, 'ui.transcript.plan')}: {plan.goal}", border_style="cyan"))


def _print_plan_details(plan: Plan, *, language: UiLanguage = "zh") -> None:
    for i, batch in enumerate(plan.batches, 1):
        console.print(Text(f"── {translate(language, 'ui.plan.round_label', number=i)}: {', '.join(batch)}", style="cyan"))
        for tid in batch:
            t = plan.task_by_id(tid)
            assert t is not None
            deps = translate(language, "ui.plan.dependency", dependencies=", ".join(t.deps)) if t.deps else ""
            console.print(Text(f"  {t.id} {t.title}{deps}", style="bold"))
            console.print(Text(f"    {t.description}", style="dim"))


def _print_plan_summary(plan: Plan, *, language: UiLanguage = "zh") -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(justify="center", no_wrap=True)
    table.add_column()
    status_style = {"done": "green", "failed": "red", "pending": "dim", "running": "yellow"}
    for t in plan.tasks:
        label = {"done": "done", "failed": "FAIL", "pending": translate(language, "ui.plan.no_output")}.get(t.status, t.status)
        table.add_row(t.id, Text(label, style=status_style.get(t.status, "dim")), t.title)
        if t.result:
            preview = t.result.strip().splitlines()[0] if t.result.strip() else ""
            if len(preview) > 120:
                preview = preview[:120] + " ..."
            if preview:
                table.add_row("", "", Text(preview, style="dim"))
    console.print(Panel(table, title=translate(language, "ui.plan.done_title"), border_style="green"))


def _render_plan_event(event: PlanEvent, *, language: UiLanguage = "zh") -> None:
    """渲染计划事件流（进度行 + 汇总面板）。"""
    task = event.task
    if event.kind == "plan_generated":
        _print_plan_panel(event.plan, note=event.message, language=language)
    elif event.kind == "review":
        pass  # 审阅交互由 PlanReviewUI 负责
    elif event.kind == "approved":
        console.print(Text(translate(language, "ui.plan.approved"), style="green"))
    elif event.kind == "cancelled":
        msg = event.message or translate(language, "ui.operation.cancelled")
        console.print(Text(translate(language, "ui.plan.cancelled", message=msg), style="yellow"))
    elif event.kind == "replanned":
        console.print(Text(translate(language, "ui.plan.replanned", message=event.message), style="dim"))
    elif event.kind == "batch_started":
        console.print(Text(f"── {event.message}: {', '.join(event.batch)}", style="cyan"))
    elif event.kind == "subtask_started" and task:
        console.print(Text(f"▶ {task.id} {task.title}", style="dim"))
    elif event.kind == "subtask_event" and task and event.agent_event:
        _render_subtask_event(task, event.agent_event, language=language)
    elif event.kind == "subtask_done" and task:
        preview = task.result.strip().splitlines()[0] if task.result.strip() else translate(language, "ui.plan.no_output")
        if len(preview) > 200:
            preview = preview[:200] + " ..."
        console.print(Text(f"OK {task.id} {task.title}: {preview}", style="green"))
    elif event.kind == "subtask_failed" and task:
        console.print(Text(f"FAIL {task.id} {task.title}: {task.result}", style="red"))
    elif event.kind == "plan_done":
        if event.plan is not None:
            _print_plan_summary(event.plan, language=language)
        console.print(Text(event.message, style="green"))
    elif event.kind == "plan_failed":
        console.print(Panel(Text(event.message), title=translate(language, "ui.plan.failed_title"), border_style="red"))


def _print_team_panel(plan: TeamPlan, note: str = "", *, language: UiLanguage = "zh") -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="magenta", justify="right", no_wrap=True)
    table.add_column()
    for index, batch in enumerate(plan.batches, 1):
        table.add_row(translate(language, "ui.plan.round_label", number=index), Text(", ".join(batch), style="bold"))
        for task_id in batch:
            task = plan.task_by_id(task_id)
            assert task is not None
            deps = translate(language, "ui.plan.dependency", dependencies=", ".join(task.deps)) if task.deps else ""
            table.add_row("", f"{task.id} [{task.owner_role}] {task.title}{deps}")
            table.add_row("", Text(f"    {translate(language, 'ui.plan.resource_mode', mode=task.resource_scope_mode)}", style="dim"))
            if getattr(task, "allowed_tools", ()):
                table.add_row("", Text(f"    {translate(language, 'ui.plan.tools', tools=', '.join(task.allowed_tools))}", style="dim"))
            for warning in getattr(task, "tool_warnings", ()):
                table.add_row("", Text(f"    {translate(language, 'ui.plan.tool_hint', warning=warning)}", style="yellow"))
            for claim in task.resource_claims:
                table.add_row("", Text(f"    {translate(language, 'ui.plan.resource_claim', access=claim.access, pattern=claim.pattern)}", style="dim"))
            criteria = "；".join(task.acceptance_criteria[:2])
            if criteria:
                table.add_row("", Text(f"    {translate(language, 'ui.plan.acceptance', criteria=criteria)}", style="dim"))
    lines = [table]
    if note:
        lines.append(Text(translate(language, "ui.plan.hint", text=note), style="yellow"))
    console.print(Panel(*lines, title=f"{translate(language, 'ui.team.plan_title')}: {plan.goal}", border_style="magenta"))


def _render_team_event(event: TeamEvent, *, language: UiLanguage = "zh") -> None:
    """渲染 Team 事件，保留角色、任务和审查身份。"""
    task = event.task
    if event.kind == "team_plan_generated" and event.plan:
        _print_team_panel(event.plan, note=event.message, language=language)
    elif event.kind == "approved":
        console.print(Text(translate(language, "ui.team.approved"), style="green"))
    elif event.kind == "cancelled":
        console.print(Text(translate(language, "ui.team.cancelled", message=event.message), style="yellow"))
    elif event.kind == "replanned":
        console.print(Text(translate(language, "ui.team.replanned", message=event.message), style="dim"))
    elif event.kind == "batch_started":
        console.print(Text(f"── {event.message}: {', '.join(event.batch)}", style="cyan"))
    elif event.kind == "task_retry_started" and task:
        console.print(Text(
            f"RETRY [{event.role or task.owner_role}/{task.id}] "
            f"{translate(language, 'ui.team.retry', attempt=event.attempt, steps=event.retry_steps, artifacts=len(event.preserved_artifacts))}",
            style="yellow",
        ))
    elif event.kind in {"task_started", "agent_started"} and task:
        console.print(Text(f"▶ [{event.role}/{task.id}] {task.title}", style="dim"))
    elif event.kind == "subtask_event" and task and event.agent_event:
        _render_subtask_event(task, event.agent_event, role=event.role, language=language)
    elif event.kind == "artifact_produced" and event.artifact:
        summary = event.artifact.summary.replace("\n", " ")[:160]
        console.print(Text(f"  [{event.role}/{task.id if task else ''}] Artifact {event.artifact.kind}: {summary}", style="dim cyan"))
    elif event.kind == "task_review_started" and task:
        console.print(Text(f"  [reviewer/{task.id}] {translate(language, 'ui.team.review_started')}", style="yellow"))
    elif event.kind == "review_output_invalid" and task:
        console.print(Text(
            f"  [reviewer/{task.id}] {translate(language, 'ui.team.review_invalid', category=event.failure_category, message=event.message[:240])}",
            style="yellow",
        ))
    elif event.kind == "review_output_retry" and task:
        console.print(Text(f"  [reviewer/{task.id}] {event.message}", style="yellow"))
    elif event.kind == "task_review_done" and task and event.review:
        style = "green" if event.review.verdict == "pass" else "red"
        detail = "；".join(event.review.findings) or translate(language, "ui.team.accepted")
        console.print(Text(f"  [reviewer/{task.id}] {event.review.verdict}: {detail[:240]}", style=style))
    elif event.kind == "repair_requested" and task:
        console.print(Text(f"  [repairer/{task.id}] {event.message[:240]}", style="yellow"))
    elif event.kind in {"task_needs_input", "repair_scope_required"} and task:
        scope = ", ".join(claim.pattern for claim in event.scope_claims) or translate(language, "ui.config.not_configured")
        console.print(Text(
            f"INPUT [{task.id}] {event.message[:240]}；"
            f"{translate(language, 'ui.team.input', scope=scope, count=event.repair_attempts_started)}",
            style="yellow",
        ))
    elif event.kind == "task_blocked" and task:
        console.print(Text(f"BLOCKED [{event.role or task.owner_role}/{task.id}]: {event.message[:240]}", style="yellow"))
    elif event.kind == "task_done" and task:
        console.print(Text(f"OK [{task.owner_role}/{task.id}]: {event.message[:200]}", style="green"))
    elif event.kind in {"task_failed", "agent_failed"} and task:
        category = f" ({event.failure_category})" if event.failure_category else ""
        console.print(Text(f"FAIL [{event.role or task.owner_role}/{task.id}]{category}: {event.message[:240]}", style="red"))
    elif event.kind == "team_done":
        console.print(Panel(Text(event.message), title=translate(language, "ui.team.done_title"), border_style="green"))
    elif event.kind == "team_failed":
        console.print(Panel(Text(event.message), title=translate(language, "ui.team.failed_title"), border_style="red"))


def _render_subtask_event(
    task: PlanTask | TeamTask,
    ae: AgentEvent,
    role: str = "",
    *,
    language: UiLanguage = "zh",
) -> None:
    """渲染子任务内部转发的 AgentEvent（前缀子任务 id）。"""
    prefix = f"  [{role + '/' if role else ''}{task.id}]"
    if ae.kind == "retrying":
        console.print(Text(
            f"{prefix} {translate(language, 'ui.cli.retry', current=ae.retry_attempts, total=ae.retry_max_attempts, seconds=ae.retry_delay or 0)}",
            style="yellow",
        ))
    elif ae.kind == "context_compacted":
        console.print(Text(f"{prefix} {ae.text}", style="dim cyan"))
    elif ae.kind == "context_warning":
        console.print(Text(f"{prefix} {translate(language, 'ui.cli.context_warning', text=ae.text)}", style="yellow"))
    elif ae.kind == "tool_call" and ae.tool_call:
        console.print(Text(f"{prefix} → {ae.tool_call.name}({ae.tool_call.arguments})", style="dim cyan"))
    elif ae.kind == "approval" and ae.tool_call:
        style = {"approved": "green", "modified": "yellow", "rejected": "red"}.get(ae.text, "yellow")
        console.print(Text(f"{prefix} {ae.text}: {ae.tool_call.name}", style=style))
    elif ae.kind == "tool_result" and ae.tool_result:
        style = "green" if ae.tool_result.ok else "red"
        preview = (ae.tool_result.output or ae.tool_result.error).strip()
        if len(preview) > 200:
            preview = preview[:200] + " ..."
        console.print(Text(f"{prefix} {'OK' if ae.tool_result.ok else 'FAIL'}: {preview}", style=style))


async def handle_plan_turn(
    agent: ReActAgent,
    settings: Settings,
    goal: str,
    session: PromptSession[str],
    approval_ui: ApprovalUI | None = None,
) -> None:
    """执行 /plan 全流程并渲染事件流。"""
    if approval_ui is not None:
        approval_ui.unbind_live()  # 计划模式下无 Live，避免误停旧实例
    executor = PlanExecutor(
        llm=agent.llm,
        tools=agent.tools,
        settings=settings,
        reviewer=PlanReviewUI(session, language=settings.ui_language),
        approval_policy=agent.approval_policy,
        audit=agent.audit,
        memory_manager=agent.memory_manager,
        mcp_manager=getattr(agent, "mcp_manager", None),
    )
    try:
        async for event in executor.run(goal):
            _render_plan_event(event, language=normalize_language(settings.ui_language))
    except LlmError as e:
        console.print(Panel(Text(translate(normalize_language(settings.ui_language), "ui.cli.request_failed", error=e)), style="red"))


async def handle_team_turn(
    agent: ReActAgent,
    settings: Settings,
    goal: str,
    session: PromptSession[str],
    approval_ui: ApprovalUI | None = None,
) -> None:
    """执行 /team 全流程并渲染角色、Artifact 和审查事件。"""
    if approval_ui is not None:
        approval_ui.unbind_live()
    executor = TeamExecutor(
        llm=agent.llm,
        tools=agent.tools,
        settings=settings,
        reviewer=PlanReviewUI(session, label="team", language=settings.ui_language),
        approval_policy=agent.approval_policy,
        audit=agent.audit,
        memory_manager=agent.memory_manager,
        mcp_manager=getattr(agent, "mcp_manager", None),
        project_root=getattr(agent.memory_manager, "project_root", None),
    )
    try:
        async for event in executor.run(goal):
            _render_team_event(event, language=normalize_language(settings.ui_language))
    except LlmError as exc:
        console.print(Panel(Text(translate(normalize_language(settings.ui_language), "ui.cli.request_failed", error=exc)), style="red"))


async def run_loop(agent: ReActAgent, settings: Settings, manager: ConfigManager) -> None:
    mcp_manager = getattr(agent, "mcp_manager", None)
    if mcp_manager is not None:
        await mcp_manager.ensure_started()
        if mcp_manager.config_errors:
            for error in mcp_manager.config_errors:
                console.print(Text(translate(normalize_language(settings.ui_language), "ui.mcp.config_notice", error=error), style="yellow"))
    try:
        await _run_loop_body(agent, settings, manager)
    finally:
        if mcp_manager is not None:
            await mcp_manager.close()
        for service_name in ("web_search", "web_fetch"):
            service = getattr(agent, service_name, None)
            if service is not None:
                await service.close()


async def _run_loop_body(agent: ReActAgent, settings: Settings, manager: ConfigManager) -> None:
    input_history = getattr(agent, "input_history", None)
    history_adapter = PromptToolkitHistory(input_history) if input_history is not None else None
    session: PromptSession[str] = PromptSession(history=history_adapter)
    language = normalize_language(settings.ui_language)
    console.print(Panel(
        f"[XG] Agent CLI v{__version__}\n{translate(language, 'ui.banner')}",
        title="XG", border_style="cyan",
    ))
    approval_ui = ApprovalUI(session, language=language)
    if agent.approval_policy is not None:
        agent.approval_policy.requester = approval_ui
    ask_ui = AskUI(session, language=language)
    agent.ask_requester = ask_ui

    # SmartRouter 跨轮路由状态（phase-01 步骤 D）
    prev_tier: str | None = None
    prev_ts: float | None = None
    # SmartRouter 反馈采集（phase-03 步骤 B）
    _feedback = FeedbackRecorder(session=str(Path.cwd()))
    # SmartRouter 校准/自学习/稳定层（phase-03 C + phase-04 A1/A2/A3）
    # 挂到 agent 上，供路由与 /smartRouter reset 重建共享同一份状态
    from xg.adaptive.calibrate import recalibrate
    from xg.adaptive.learned_rules import re_learn
    from xg.router.postprocess import Hysteresis
    from xg.router.ml_router import MLRouter
    from xg.router.semantic import load_semantic_encoder

    agent._smart_calibration = recalibrate()
    agent._smart_learned = re_learn()
    agent._smart_hysteresis = Hysteresis()
    agent._smart_ml = MLRouter(semantic=load_semantic_encoder())  # 第6期 C2：语义列可选，缺则回落
    _calibration = agent._smart_calibration  # 兼容旧引用

    while True:
        try:
            if history_adapter is not None:
                history_adapter.recording_enabled = True
            user_input = await session.prompt_async(HTML("<ansicyan>xg ></ansicyan> "))
        except (KeyboardInterrupt, EOFError):
            console.print(translate(language, "ui.cli.goodbye"))
            return
        finally:
            if history_adapter is not None:
                history_adapter.recording_enabled = False

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/plan"):
            goal = user_input[5:].strip()
            if not goal:
                console.print(Text(translate(language, "ui.plan.usage", command="/plan"), style="yellow"))
                continue
            try:
                await handle_plan_turn(agent, settings, goal, session, approval_ui)
            except KeyboardInterrupt:
                console.print(Text(translate(language, "ui.cli.plan_interrupted"), style="yellow"))
            continue

        if user_input.startswith("/team"):
            goal = user_input[5:].strip()
            if not goal:
                console.print(Text(translate(language, "ui.plan.usage", command="/team"), style="yellow"))
                continue
            try:
                await handle_team_turn(agent, settings, goal, session, approval_ui)
            except KeyboardInterrupt:
                console.print(Text(translate(language, "ui.cli.plan_interrupted"), style="yellow"))
            continue

        if user_input.lower().startswith(("/init", "/save", "/memory")):
            try:
                message = await _handle_memory_command(agent, user_input, session, language=language)
            except KeyboardInterrupt:
                message = translate(language, "ui.cli.memory_interrupted")
            if message:
                console.print(Text(message, style="dim"))
            continue

        if user_input.startswith("/ask"):
            goal = user_input[5:].strip()
            if not goal:
                console.print(Text(translate(language, "ui.plan.usage", command="/ask"), style="yellow"))
                continue
            asked = (
                "这是用户通过 /ask 发起的任务，要求你执行前先向用户确认关键信息。\n"
                "规则：开始执行任何工具前，先调用 ask_user 向你提问，用问题+选项收齐"
                "范围、约束、取舍等关键不确定点；用户回答后再依次执行。除非确实没有需要"
                "澄清的地方，否则不要直接跳过。\n"
                f"任务如下：\n{goal}"
            )
            try:
                await handle_turn(agent, asked)
            except KeyboardInterrupt:
                console.print(Text(translate(language, "ui.cli.interrupted"), style="yellow"))
            continue

        if user_input.startswith("/"):
            if user_input.split(maxsplit=1)[0].lower() == "/mcp":
                from xg.cli.commands import execute_mcp_command

                message, _ = await execute_mcp_command(agent, user_input, language=language)
                should_exit = False
            elif user_input.split(maxsplit=1)[0].lower() == "/web":
                from xg.cli.commands import execute_web_command

                message, should_exit = await execute_web_command(agent, user_input, language=language)
            elif user_input.split(maxsplit=1)[0].lower() == "/skill":
                from xg.cli.commands import execute_skill_command

                message, should_exit = await execute_skill_command(agent, user_input, language=language)
            elif user_input.split(maxsplit=1)[0].lower() == "/history":
                from xg.cli.commands import execute_history_command

                message, should_exit = await execute_history_command(agent, user_input, language=language)
            elif user_input.split(maxsplit=1)[0].lower() == "/provider":
                from xg.cli.commands import execute_provider_command

                message, _ok = execute_provider_command(manager, settings, user_input, language=language)
            elif user_input.split(maxsplit=1)[0].lower() == "/tier":
                from xg.cli.commands import execute_tier_command

                message, _ok = execute_tier_command(manager, settings, user_input, language=language)
            elif user_input.split(maxsplit=1)[0].lower() == "/path":
                from xg.cli.commands import execute_path_command

                message = execute_path_command(user_input, language=language)[0]
            elif user_input.split(maxsplit=1)[0].lower() == "/train":
                message = _run_train_inline(user_input, language=language)
            else:
                message, should_exit = _handle_command(agent, settings, manager, user_input)
            if message:
                console.print(Text(message, style="dim"))
            if should_exit:
                return
            continue

        try:
            if settings.smart_router_enabled:
                prev_tier, prev_ts = _route_user_turn(
                    agent, settings, manager, user_input, prev_tier, prev_ts,
                    feedback=_feedback,
                    calibration=agent._smart_calibration,
                    learned_rules=agent._smart_learned,
                    hysteresis=agent._smart_hysteresis,
                    ml_router=agent._smart_ml,
                    language=language,
                )
            await handle_turn(agent, user_input, approval_ui, language=language)
        except KeyboardInterrupt:
            # phase-03 步骤 B：回答问题中途 Ctrl+C 视为"该档不够强"
            if settings.smart_router_enabled:
                if capture_interrupt(_feedback, prev_tier):
                    _feedback.flush()
            console.print(Text(translate(language, "ui.cli.task_interrupted"), style="yellow"))
        except LlmError as e:
            console.print(Panel(Text(translate(language, "ui.cli.request_failed", error=e)), style="red"))


def _run_train_inline(raw: str, *, language: UiLanguage = "zh") -> str | None:
    """inline 版 /train：确认 + 同步阻塞逐行打印训练日志，返回汇总消息。

    训练是显式手动触发：不带 --yes 只回确认提示；带 --yes 才 spawn 阻塞运行，
    每行即时打印到 stdout（实时进度），结束后返回简短汇总。
    """
    from xg.cli.train import (  # noqa: PLC0415
        check_train_deps,
        confirmation_message,
        parse_train_command,
        run_training_sync,
    )

    plan, err = parse_train_command(raw, language=language)
    if err:
        return err
    dep_err = check_train_deps(language=language)
    if dep_err:
        return dep_err
    if not plan.overwrite:
        return confirmation_message(plan, language=language)

    ok, lines = run_training_sync(plan, on_line=lambda line: console.print(Text(line, style="dim")))
    # 行日志已逐行实时上屏，这里只返回简短收尾，避免重复。
    if ok:
        return translate(language, "ui.command.train_done")
    return translate(language, "ui.command.train_failed")


def _handle_command(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, raw: str
) -> tuple[str | None, bool]:
    """处理斜杠命令。返回 (输出消息, 是否退出程序)。"""
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    language = normalize_language(getattr(settings, "ui_language", "zh"))

    if cmd in ("/help", "/?"):
        return (format_command_help(arg, language=language) if arg else format_help(language=language)), False
    if cmd in ("/exit", "/quit"):
        return translate(language, "ui.cli.goodbye"), True
    if cmd in ("/cancel", "/c"):
        return translate(language, "ui.command.inline_cancel"), False
    if cmd == "/clear":
        agent.clear()
        return translate(language, "ui.command.context_cleared"), False
    if cmd in ("/lang", "/language"):
        from xg.cli.commands import _execute_language_command

        result = _execute_language_command(settings, manager, raw)
        return result.message, False
    if cmd == "/model":
        return _cmd_model(agent, settings, manager, arg, language=language), False
    if cmd == "/smartrouter":
        return _cmd_smart_router(agent, settings, manager, arg, language=language), False
    if cmd == "/config":
        return _cmd_config(agent, settings, manager, arg, language=language), False
    if cmd == "/provider":
        from xg.cli.commands import execute_provider_command

        message, ok = execute_provider_command(manager, settings, raw, language=normalize_language(settings.ui_language))
        if ok:
            _reapply_active(agent, settings, manager)
        return message, False
    if cmd == "/hitl":
        return _cmd_hitl(agent, arg, language=language), False
    if cmd == "/save":
        return _cmd_memory_sync(agent, cmd, arg, language=language), False
    if cmd == "/memory":
        return _cmd_memory_sync(agent, cmd, arg, language=language), False
    if cmd == "/mcp":
        mcp = getattr(agent, "mcp_manager", None)
        return (mcp.format_status() if mcp is not None else translate(language, "ui.command.mcp_uninitialized")), False
    from xg.cli.commands import SLASH_COMMANDS

    names = " ".join(spec.name for spec in SLASH_COMMANDS)
    return translate(language, "ui.command.unknown", command=cmd, commands=names), False


def _memory_manager(agent: ReActAgent) -> MemoryManager | None:
    return getattr(agent, "memory_manager", None)


def _format_memory_entries(entries) -> str:
    if not entries:
        return "没有找到长期记忆。"
    lines = []
    for entry in entries:
        timestamp = entry.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        preview = " ".join(entry.content.split())
        if len(preview) > 120:
            preview = preview[:120] + " ..."
        lines.append(f"#{entry.id}  {timestamp}  {preview}")
    return "\n".join(lines)


def _cmd_memory_sync(agent: ReActAgent, cmd: str, arg: str, *, language: UiLanguage = "zh") -> str:
    memory = _memory_manager(agent)
    if memory is None:
        return translate(language, "ui.memory.uninitialized")
    try:
        if cmd == "/save":
            if not arg:
                return translate(language, "ui.memory.save_usage")
            entry, created, redacted = memory.save(arg)
            action = "已保存" if created else "已存在，已刷新时间"
            suffix = "（敏感片段已脱敏）" if redacted else ""
            return translate(language, "ui.memory.saved" if created else "ui.memory.refreshed", id=entry.id, suffix=suffix)

        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "list":
            limit = 20
            if rest:
                try:
                    limit = int(rest)
                except ValueError:
                    return translate(language, "ui.memory.list_usage")
            return _format_memory_entries(memory.list(limit))
        if sub == "search":
            if not rest:
                return translate(language, "ui.memory.search_usage")
            return _format_memory_entries(memory.search(rest))
        if sub == "delete":
            try:
                memory_id = int(rest)
            except ValueError:
                return translate(language, "ui.memory.delete_usage")
            return translate(language, "ui.memory.deleted", id=memory_id) if memory.delete(memory_id) else translate(language, "ui.memory.not_found", id=memory_id)
        if sub == "clear":
            return translate(language, "ui.memory.clear_needs_confirmation")
        return translate(language, "ui.memory.usage")
    except (MemoryUnavailableError, OSError, ValueError) as exc:
        return translate(language, "ui.memory.failed", error=exc)


async def _handle_memory_command(
    agent: ReActAgent, raw: str, session: PromptSession[str], *, language: UiLanguage = "zh"
) -> str:
    """处理需要 prompt 的第五期命令；普通 list/search/save 仍走同步逻辑。"""
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    memory = _memory_manager(agent)
    if memory is None:
        return translate(language, "ui.memory.uninitialized")

    if cmd == "/init":
        try:
            draft = await memory.generate_init_draft(agent.llm)
        except FileExistsError as exc:
            return str(exc)
        except (LlmError, OSError, ValueError) as exc:
            return translate(language, "ui.error.memory_draft", error=exc)
        console.print(Panel(Markdown(draft), title=translate(language, "ui.memory.write_title"), border_style="cyan"))
        answer = await session.prompt_async(
            HTML(f"<ansiyellow>{translate(language, 'ui.memory.confirm_write')} ></ansiyellow> ")
        )
        if answer.strip().lower() not in ("y", "yes"):
            return translate(language, "ui.operation.cancelled")
        try:
            path = memory.write_init_draft(draft)
        except (FileExistsError, OSError) as exc:
            return translate(language, "ui.operation.failed", error=exc)
        return translate(language, "ui.operation.memory_written", name=path.name)

    if cmd == "/memory" and arg.lower() == "clear":
        try:
            count = memory.count()
        except (MemoryUnavailableError, OSError) as exc:
            return translate(language, "ui.memory.failed", error=exc)
        if count == 0:
            return translate(language, "ui.memory.no_entries")
        answer = await session.prompt_async(
            HTML(f"<ansiyellow>{translate(language, 'ui.memory.clear_body', count=count)}\n{translate(language, 'ui.memory.confirm_clear')} ></ansiyellow> ")
        )
        if answer.strip().lower() != "clear":
            return translate(language, "ui.operation.cancelled")
        try:
            removed = memory.clear()
        except (MemoryUnavailableError, OSError) as exc:
            return translate(language, "ui.memory.failed", error=exc)
        return translate(language, "ui.operation.memory_cleared", count=removed)

    return _cmd_memory_sync(agent, cmd, arg, language=language)


def _cmd_hitl(agent: ReActAgent, arg: str, *, language: UiLanguage = "zh") -> str:
    policy = getattr(agent, "approval_policy", None)
    if policy is None:
        return translate(language, "ui.hitl.uninitialized")
    sub = arg.split()[0].lower() if arg.split() else ""
    if sub == "on":
        policy.set_enabled(True)
        return translate(language, "ui.hitl.enabled")
    if sub == "off":
        policy.set_enabled(False)
        policy.reset_session()
        return translate(language, "ui.hitl.disabled")
    if sub == "reset":
        policy.reset_session()
        return translate(language, "ui.hitl.reset")
    status = translate(language, "ui.tier.enabled" if policy.enabled else "ui.tier.disabled")
    allow_all = "Yes" if policy.session_allow_all and language == "en" else "No" if language == "en" else "是" if policy.session_allow_all else "否"
    return translate(language, "ui.hitl.status", status=status, allow_all=allow_all)


def _cmd_model(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    """/model：list 查看；否则在当前 base provider 内切模型（限模型列表内）。"""
    if not arg or arg.strip().lower() == "list":
        return _model_catalog(manager, language=language)

    model = arg.strip()
    provider = manager.resolve_provider(settings.provider) if settings.provider else None
    if provider is None:
        return translate(language, "ui.model.no_provider", catalog=_model_catalog(manager, language=language))

    allowed = [provider.default_model, *provider.models]
    if model not in allowed:
        available = ", ".join(allowed) if language == "en" else "、".join(allowed) if allowed else "（无）"
        return translate(language, "ui.model.not_allowed", model=model, provider=provider.name, available=available)

    result = _switch(agent, settings, manager, provider.name, model, language=language)

    # 手动优先接管：/model 切换成功即持久化并关闭 SmartRouter 清除快照
    if result == translate(language, "ui.provider.switched", provider=provider.display_name, model=model):
        manager.set_config_value("active_model", model)
        _disable_smart_router(settings, manager)
        return result + translate(language, "ui.model.switched_router_off")
    return result


def _cmd_smart_router(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    """处理 /smartRouter on|off|status。default status。"""
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "status"

    if sub in ("on", "enable"):
        if settings.smart_router_enabled:
            return translate(language, "ui.router.already_enabled")
        settings.smart_router_saved = (settings.provider, settings.model)
        settings.smart_router_enabled = True
        manager.set_smart_router_enabled(True)
        return translate(language, "ui.router.enabled_detail", provider=settings.provider, model=settings.model)

    if sub in ("off", "disable"):
        if not settings.smart_router_enabled:
            return translate(language, "ui.router.already_disabled")
        settings.smart_router_enabled = False
        manager.set_smart_router_enabled(False)
        saved = settings.smart_router_saved
        settings.smart_router_saved = None
        if saved:
            _switch(agent, settings, manager, saved[0], saved[1], language=language)
        return translate(language, "ui.router.disabled_detail")

    if sub in ("reset", "clear"):
        from xg.adaptive.store import reset_adaptive_data
        from xg.adaptive.calibrate import recalibrate
        from xg.adaptive.learned_rules import re_learn
        from xg.router.postprocess import Hysteresis
        from xg.router.ml_router import MLRouter

        removed = reset_adaptive_data()
        # 重建内存共享状态，使 reset 立即生效（不再用旧校准/规则）
        agent._smart_calibration = recalibrate()
        agent._smart_learned = re_learn()
        agent._smart_hysteresis = Hysteresis()
        from xg.router.semantic import load_semantic_encoder
        agent._smart_ml = MLRouter(semantic=load_semantic_encoder())  # 与主循环一致
        detail = ", ".join(removed) if removed and language == "en" else "、".join(removed) if removed else ("(nothing to clear this round)" if language == "en" else "（本轮无可清除项）")
        return translate(language, "ui.router.reset", detail=detail)

    if sub == "status" or arg.strip() in ("status", ""):
        lines = [translate(language, "ui.router.status", status="Enabled" if settings.smart_router_enabled else "Disabled" if language == "en" else "开启" if settings.smart_router_enabled else "关闭")]
        cfg = manager.smart_router_config()
        tiers = cfg.get("tiers") or {}
        for idx, name in enumerate(TIER_NAMES):
            target = resolve_tier(idx, settings.provider, settings.model, tiers, manager)
            raw_entry = tiers.get(name)
            if target.configured:
                mark = "OK"
            else:
                mark = "(x)" if raw_entry else "-"
            lines.append(translate(language, "ui.router.tier_line", name=name, provider=target.provider, model=target.model, mark=mark))
        lines.append(translate(language, "ui.router.legend"))
        # 校准状态（phase-03 步骤 C）：直接聚合 feedback.log 展示实时样本
        from xg.adaptive.calibrate import aggregate
        from xg.adaptive.feedback import read_feedback
        from xg.adaptive.learned_rules import load_learned_rules, rule_hit_stats

        records = read_feedback()
        cal = aggregate(records)
        parts = [f"{name}={cal.samples[i]:g}" for i, name in enumerate(TIER_NAMES)]
        lines.append(translate(language, "ui.router.calibration", values=" ".join(parts)))
        bias_parts = [
            f"{name}={cal.bias[i]:+.2f}" for i, name in enumerate(TIER_NAMES)
        ]
        lines.append(translate(language, "ui.router.bias", values=" ".join(bias_parts), threshold=cal.threshold_adjust))
        # 自学习规则（phase-04 A1/A3）：规则数量与在 feedback.log 上的命中情况
        rules = getattr(agent, "_smart_learned", load_learned_rules())
        stats = rule_hit_stats(records, rules)
        lines.append(
            translate(language, "ui.router.learned", count=stats['rule_count'], hits=stats['hit_records'], samples=stats['sample_records'])
        )
        for pr in stats["per_rule"]:
            pred = "、".join(f"{k}{v:g}" for k, v in pr["predicate"].items())
            lines.append(
                translate(language, "ui.router.rule", predicate=pred, action=pr['action'], confidence=pr['confidence'], support=pr['support'], hits=pr['hits'])
            )
        # ML 精判（phase-05 B2）：产物可用性观测；缺失/缺依赖时显示离线
        ml = getattr(agent, "_smart_ml", None)
        if ml is not None:
            if ml.available:
                n = ml.n_samples
                suffix = f", {n} samples" if n is not None and language == "en" else f"，样本 {n}" if n is not None else ""
                lines.append(translate(language, "ui.router.ml_available", suffix=suffix, dim=ml.sem_dim))
            else:
                lines.append(translate(language, "ui.router.ml_offline"))
            # 语义通道（phase-06 C3）：可用性/产物/耗时/有效样本 观测
            sem = ml.semantic
            if sem is not None:
                if sem.available:
                    lines.append(
                        translate(language, "ui.router.semantic_available", dim=sem.dim, calls=sem.calls, avg=sem.avg_ms, last=sem.last_ms)
                    )
                elif sem.artifact_exists:
                    lines.append(translate(language, "ui.router.semantic_broken"))
                else:
                    lines.append(translate(language, "ui.router.semantic_missing"))
        return "\n".join(lines)

    return translate(language, "ui.router.usage")


def _disable_smart_router(settings: Settings, manager: ConfigManager) -> None:
    """手动 /model 切换后关闭 SmartRouter 并清除快照（手动优先）。"""
    if not settings.smart_router_enabled:
        return
    settings.smart_router_enabled = False
    settings.smart_router_saved = None
    manager.set_smart_router_enabled(False)


def _attach_model(
    settings: Settings, manager: ConfigManager, agent: ReActAgent,
    provider_name: str, model: str,
) -> str | None:
    """仅重建 agent.llm 与内存配置（provider/model/base），不写回持久化。

    SmartRouter 路由用——避免把自动路由到的模型写进 active_provider/active_model。
    返回错误消息；成功返回 None。
    """
    provider = manager.resolve_provider(provider_name)
    if provider is None:
        return translate(settings.ui_language, "ui.provider.unknown", name=provider_name)
    key = manager.resolve_api_key(provider)
    if not key:
        return (f"Missing {provider.name} api_key configuration; use /provider key {provider.name} <KEY> to write config.json." if normalize_language(settings.ui_language) == "en" else f"缺少 {provider.name} 的 api_key 配置，无法使用。请用 /provider key {provider.name} <KEY> 写入 config.json。")
    agent.llm = create_client(
        manager.resolve_api_base(provider), key, model,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    settings.provider = provider.name
    settings.model = model
    settings.api_base = manager.resolve_api_base(provider)
    settings.api_key = key
    settings.context_window = manager.resolve_window(provider)
    return None


def _route_user_turn(
    agent: ReActAgent, settings: Settings, manager: ConfigManager,
    user_input: str, prev_tier: str | None, prev_ts: float | None,
    feedback: FeedbackRecorder | None = None,
    calibration=None,
    learned_rules=None,
    hysteresis=None,
    ml_router=None,
    language: UiLanguage = "zh",
) -> tuple[str, float]:
    """对一轮普通输入做路由并切换到目标模型（不持久化），打出行内日志。

    返回 (final_tier, ts)，供下一轮作为防降级上下文。
    传入 ``feedback`` 时采集 clarify / cmd_retry / short_high_tier 反馈（phase-03 步骤 B）。
    传入 ``calibration`` 时在规则打分后应用档位偏置与置信门（phase-03 步骤 C）。
    传入 ``learned_rules`` / ``hysteresis`` 分别启用局部规则微调与会话内迟滞
    （phase-04 A1/A2，均由 /smartRouter reset 重建的共享状态提供）。
    传入 ``ml_router``（phase-05 B2）时软规则决策参与 ML 精判，不可用/信心
    不足静默回落规则档位。
    """
    tiers = (manager.smart_router_config().get("tiers") or {})
    now = time.time()
    result = route_turn(
        user_input,
        prev_tier=prev_tier, prev_ts=prev_ts, ts=now,
        fallback_provider=settings.provider, fallback_model=settings.model,
        tiers_config=tiers, manager=manager, calibration=calibration,
        learned_rules=learned_rules, hysteresis=hysteresis, ml_router=ml_router,
    )
    if feedback is not None:
        capture_turn_signals(
            feedback, user_input, result.features, prev_tier, result.tier, TIER_NAMES,
        )
        feedback.flush()
    if (result.provider, result.model) != (settings.provider, settings.model):
        err = _attach_model(settings, manager, agent, result.provider, result.model)
        if err:
            console.print(Text(translate(language, "ui.router.route_failed", error=err), style="dim"))
            return prev_tier or result.tier, now
    console.print(
        Text(
            f"→ SmartRouter: {result.tier} → {result.provider}/{result.model}",
            style="dim",
        )
    )
    return result.tier, now


def _model_catalog(manager: ConfigManager, *, language: UiLanguage = "zh") -> str:
    """Render the current model and the available providers catalog."""
    active = manager.active()
    lines = [
        translate(language, "ui.model.current", provider=active.provider_name, model=active.model, window=active.context_window),
        translate(language, "ui.model.providers"),
    ]
    for p in manager.list_providers():
        models = ", ".join(p.models) if p.models else ("(none)" if language == "en" else "（无）")
        cache = "cache" if p.supports_cache else "-"
        vision = "vision" if p.supports_vision else "-"
        lines.append(
            f"  {p.name:<10} default={p.default_model}  models=[{models}]  "
            f"{'window' if language == 'en' else '窗口'} {p.context_window:<6} {cache:<5} {vision}"
        )
    return "\n".join(lines)


def _switch(
    agent: ReActAgent,
    settings: Settings,
    manager: ConfigManager,
    provider_name: str,
    model: str | None,
    *, language: UiLanguage = "zh",
) -> str:
    """切换到指定 provider（可选指定模型）。失败返回错误消息，不改变现状。"""
    provider = manager.resolve_provider(provider_name)
    if provider is None:
        return translate(language, "ui.provider.unknown_available", name=provider_name, available=', '.join(manager.provider_names()))
    key = manager.resolve_api_key(provider)
    if not key:
        return (f"Missing {provider.name} api_key configuration; use /provider key {provider.name} <KEY> to write config.json." if language == "en" else f"缺少 {provider.name} 的 api_key 配置，无法切换到 {provider.name}。请用 /provider key {provider.name} <KEY> 写入 config.json。")
    model = model or provider.default_model

    api_base = manager.resolve_api_base(provider)
    settings.provider = provider.name
    settings.model = model
    settings.api_base = api_base
    settings.api_key = key
    settings.context_window = manager.resolve_window(provider)
    agent.llm = create_client(
        api_base, key, model,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    manager.set_active(provider.name, model)
    return translate(language, "ui.provider.switched", provider=provider.display_name, model=model)


def _reapply_active(
    agent: ReActAgent, settings: Settings, manager: ConfigManager
) -> None:
    """/provider 变更后把运行中 client 同步到当前 base，配好即用、无需重启。

    仅同步到能解析的 base；key/api_base 仍缺时保持原样，由调用期引导兜底。
    """
    try:
        active = manager.active()
    except ProviderNotConfigured:
        return
    msg = _switch(agent, settings, manager, active.provider_name, active.model, language=normalize_language(settings.ui_language))
    if msg.startswith(("已切换", "Switched:")):
        console.print(Text(msg, style="dim"))


def _cmd_config(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    parts = arg.split()
    sub = parts[0].lower() if parts else ""

    if sub == "list":
        header = f"{'provider':<14}{'default model' if language == 'en' else '默认模型':<20}{'window' if language == 'en' else '窗口':<8}cache  vision"
        lines = [header]
        for p in manager.list_providers():
            lines.append(
                f"{p.name:<14}{p.default_model:<20}{p.context_window:<8}"
                f"{'✓' if p.supports_cache else '-'}     "
                f"{'✓' if p.supports_vision else '-'}"
            )
        return "\n".join(lines)

    if sub == "get":
        if len(parts) < 2:
            return translate(language, "ui.config.usage_get")
        value = manager.get_config_value(parts[1])
        return f"{parts[1]} = {value if value is not None else translate(language, 'ui.config.value_unset')}"

    if sub == "set":
        if len(parts) < 3:
            return translate(language, "ui.config.usage_set")
        key, value = parts[1], parts[2]
        if key == "active_provider":
            return _switch(agent, settings, manager, value, None, language=language)
        if key == "active_model":
            return _switch(agent, settings, manager, settings.provider, value, language=language)
        manager.set_config_value(key, value)
        return translate(language, "ui.config.set", config_key=key, value=value, path=manager.user_config_path)

    active = manager.active()
    return translate(
        language,
        "ui.config.overview",
        provider=active.provider_name,
        model=active.model,
        api_base=active.api_base,
        api_key=mask_key(active.api_key),
        window=active.context_window,
    ) + f"\ncache:    {'✓' if active.supports_cache else '-'}    vision: {'✓' if active.supports_vision else '-'}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xg-cli", description="XG Agent CLI")
    parser.add_argument("--tui", action="store_true", help="强制启动 Textual 全屏界面")
    parser.add_argument("--inline", action="store_true", help="使用兼容的 inline CLI")
    parser.add_argument("--no-tui", action="store_true", help="--inline 的兼容别名")
    parser.add_argument("--version", action="store_true", help="显示版本")
    return parser.parse_args(argv)


def _tui_mode(args: argparse.Namespace) -> str:
    if args.inline or args.no_tui:
        return "inline"
    if args.tui:
        return "tui"
    configured = os.environ.get("XG_TUI_MODE", "auto").lower()
    return configured if configured in {"inline", "tui"} else "auto"


def _run_tui_or_inline(agent: ReActAgent, settings: Settings, manager: ConfigManager, args: argparse.Namespace) -> None:
    mode = _tui_mode(args)
    if mode == "inline" or (mode == "auto" and not (sys.stdin.isatty() and sys.stdout.isatty())):
        asyncio.run(run_loop(agent, settings, manager))
        return
    try:
        from xg.tui.app import run_tui
        run_tui(agent, settings, manager)
    except Exception as exc:
        if mode == "tui":
            language = normalize_language(settings.ui_language)
            message = f"Textual TUI failed to start: {exc}\nUse xg --inline instead." if language == "en" else f"Textual TUI 启动失败：{exc}\n请改用 xg --inline。"
            console.print(Panel(Text(message), style="red"))
            raise SystemExit(1) from exc
        language = normalize_language(settings.ui_language)
        message = f"TUI unavailable; falling back to inline: {exc}" if language == "en" else f"TUI 不可用，已降级到 inline：{exc}"
        console.print(Text(message, style="yellow"))
        asyncio.run(run_loop(agent, settings, manager))


def _heal_path() -> bool:
    return _ensure_on_path()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.version:
        print(f"xg-cli {__version__}")
        return
    # 首次运行自愈：把命令 scripts 目录加入用户 PATH；命令文件不可用时改写启动器。
    # 可用 XG_AUTO_PATH=0 关闭；只执行一次（幂等）。
    if _heal_path():
        from xg.cli import path_heal as _ph

        _st = _ph.path_status()
        if _st.get("shim_file") and not _st.get("command_file"):
            _tip = f"已生成 XG-CLI 启动器（{_st['shim_file']}）。"
        else:
            _tip = "已将 XG-CLI 命令目录加入 PATH。"
        console.print(
            Panel(
                Text(
                    f"{_tip}请重开一个终端后直接运行: xg-cli"
                ),
                style="green",
            )
        )
    manager = ConfigManager()
    settings = load_settings(manager)
    language = normalize_language(settings.ui_language)
    if settings.provider_missing:
        console.print(
            Panel(
                Text(
                    translate(language, "ui.start.no_provider")
                ),
                style="yellow",
            )
        )
    elif not settings.api_base or not settings.api_key:
        console.print(
            Panel(
                Text(
                    translate(language, "ui.start.no_key")
                ),
                style="yellow",
            )
        )
    elif not settings.model:
        console.print(Panel(Text(translate(language, "ui.start.no_model")), style="yellow"))

    agent = build_agent(settings, config_manager=manager)
    try:
        _run_tui_or_inline(agent, settings, manager, args)
    except KeyboardInterrupt:
        console.print(translate(language, "ui.cli.goodbye"))


if __name__ == "__main__":
    main()
