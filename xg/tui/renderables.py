"""Rich renderables for transcript items."""

from __future__ import annotations

import json

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from xg.tui.diagrams import FlowchartModel, FlowchartParseError, parse_flowchart, render_flowchart, split_mermaid_blocks
from xg.tui.plan_renderables import PlanReviewCard
from xg.tui.state import AgentGroupState, TranscriptItem
from xg.tui.i18n import UiLanguage, normalize_language, translate, ui_status


def _truncate(value: str, limit: int = 20_000, language: UiLanguage = "zh") -> str:
    return value if len(value) <= limit else value[:limit] + "\n" + translate(language, "ui.transcript.truncated")


def _mermaid_source(source: str, language: UiLanguage = "zh") -> str:
    return "```mermaid\n" + _truncate(source, language=language) + "\n```"


def _trace_status(item: TranscriptItem, language: UiLanguage = "zh") -> str:
    return ui_status(language, {"streaming": "working", "running": "running"}.get(item.status, item.status))


def _trace_summary(item: TranscriptItem, language: UiLanguage = "zh") -> str:
    if item.kind == "thinking":
        label = translate(language, "ui.transcript.thinking")
    elif item.kind == "tool_call":
        label = f"{translate(language, 'ui.transcript.tool_call')} · {item.tool_name}"
    elif item.kind == "tool_result":
        label = f"{translate(language, 'ui.transcript.tool_result')} · {item.tool_name}"
    else:
        label = item.kind
    source = item.text or item.tool_args or translate(language, "ui.transcript.no_detail")
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), translate(language, "ui.transcript.no_detail"))
    return f"{label} · {_trace_status(item, language)} · {first_line[:100]}"


def trace_renderable(item: TranscriptItem, language: UiLanguage = "zh"):
    """Render a collapsible trace item; the widget owns click behavior."""
    marker = "▶" if item.collapsed else "▼"
    if item.kind == "thinking":
        label = translate(language, "ui.transcript.thinking")
        detail = _truncate(item.text, 12_000, language)
    elif item.kind == "tool_call":
        label = f"{translate(language, 'ui.transcript.tool_call')} · {item.tool_name}"
        try:
            detail = json.dumps(json.loads(item.tool_args), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            detail = item.tool_args
        detail = _truncate(detail, 4_000, language)
    elif item.kind == "tool_result":
        label = f"{translate(language, 'ui.transcript.tool_result')} · {item.tool_name}"
        detail = _truncate(item.text, 12_000, language)
    else:
        label = f"{translate(language, 'ui.transcript.approval')} · {item.tool_name}"
        detail = _truncate(item.text or translate(language, "ui.approval.title"), 2_000, language)
    body = detail if not item.collapsed else _trace_summary(item, language)
    style = {
        "streaming": "yellow",
        "running": "yellow",
        "success": "green",
        "failed": "red",
        "blocked": "yellow",
        "skipped": "cyan",
        "cancelled": "yellow",
    }.get(item.status, "cyan")
    title = f"{marker} {label} · {_trace_status(item, language)}"
    return Panel(Text(body), title=title, border_style=style)


def _agent_group_status(status: str, language: UiLanguage = "zh") -> str:
    return ui_status(language, status or "unknown") if status else translate(language, "status.unknown")


def agent_group_renderable(group: AgentGroupState, language: UiLanguage = "zh"):
    """Render one AgentRun as a collapsible group in the shared transcript."""
    marker = "▶" if group.collapsed else "▼"
    identity = f"{group.role}/{group.task_id}" if group.task_id else group.role
    stats = (
        f"{translate(language, 'ui.agent.tools')} {group.tool_count} · "
        f"{translate(language, 'ui.agent.artifacts')} {group.artifact_count}"
    )
    header = f"{marker} [{identity}] {group.task_title} · {_agent_group_status(group.status, language)} · {group.resource_scope_mode} · {stats}"
    if group.effective_steps:
        header += f" · {translate(language, 'ui.agent.budget', count=group.effective_steps)}"
    if group.attempt > 1:
        header += f" · {translate(language, 'ui.agent.attempt', count=group.attempt)}"
    if group.failure_category:
        header += f" · {group.failure_category}"
    if group.repair_attempt:
        header += f" · {translate(language, 'ui.agent.repair', count=group.repair_attempt)}"
    latest = group.latest_error or group.latest_summary
    if group.collapsed and latest:
        header += f"\n  {latest.replace(chr(10), ' ')[:180]}"
    parts: list[object] = [Text(header)]
    if not group.collapsed:
        if group.entries:
            parts.append(Group(*(render_item(item) for item in group.entries)))
        else:
            parts.append(Text(translate(language, "ui.transcript.no_events"), style="dim"))
    style = {
        "running": "yellow",
        "reviewing": "yellow",
        "repairing": "magenta",
        "done": "green",
        "failed": "red",
        "blocked": "yellow",
        "skipped": "cyan",
        "cancelled": "yellow",
    }.get(group.status, "cyan")
    return Panel(Group(*parts), title=translate(language, "ui.transcript.agent"), border_style=style)


class DiagramCard:
    """Rich renderable for one Mermaid block inside the transcript."""

    def __init__(
        self,
        source: str,
        model: FlowchartModel | None,
        *,
        error: str = "",
        source_visible: bool = False,
        language: UiLanguage = "zh",
    ) -> None:
        self.source = source
        self.model = model
        self.error = error
        self.source_visible = source_visible
        self.language = normalize_language(language)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        available = max(1, options.max_width - 4)
        if self.model is None:
            body = f"{translate(self.language, 'ui.transcript.flowchart_failed', error=self.error)}\n\n{_mermaid_source(self.source, self.language)}"
            title = "Flowchart · parse failed"
            yield Panel(Text(_truncate(body)), title=title, border_style="bright_blue")
            return
        else:
            result = render_flowchart(self.model, width=available)
            title = f"Flowchart · {self.model.direction} · {len(self.model.nodes)} nodes · {len(self.model.edges)} edges"
            if result.mode != "unicode":
                title += f" · {result.mode}"
            parts: list[object] = [Text(result.text, no_wrap=True, overflow="crop")]
            if result.warnings:
                parts.append(Text("\n\n⚠ " + "；".join(result.warnings)))
            if self.source_visible:
                parts.append(Text("\n\n" + _mermaid_source(self.source, self.language)))
            parts.append(Text("\n\n" + translate(self.language, "ui.transcript.flowchart_hint")))
            yield Panel(Group(*parts), title=title, border_style="bright_blue")
            return


def _assistant_renderable(item: TranscriptItem, language: UiLanguage = "zh"):
    parts: list[object] = []
    for text, block in split_mermaid_blocks(_truncate(item.text, language=language)):
        if block is None:
            if text:
                try:
                    parts.append(Markdown(text))
                except Exception:
                    parts.append(Text(text))
            continue
        try:
            model = parse_flowchart(block.source)
            parts.append(DiagramCard(block.source, model, source_visible=item.diagram_source_visible, language=language))
        except FlowchartParseError as exc:
            # An unsupported block remains visible as source; a malformed
            # diagram must never prevent the rest of the assistant message.
            parts.append(DiagramCard(block.source, None, error=str(exc), source_visible=True, language=language))
    if not parts:
        parts.append(Text("…"))
    return Panel(Group(*parts), title="XG", border_style="green" if not item.streaming else "yellow")


def render_item(item: TranscriptItem, language: UiLanguage = "zh"):
    language = normalize_language(language)
    if item.kind == "user":
        return Panel(Text(item.text), title=translate(language, "ui.transcript.you"), border_style="cyan")
    if item.kind == "progress":
        return Panel(
            Text(f"… {item.text}"),
            title=translate(language, "ui.transcript.working"),
            border_style="yellow",
        )
    if item.kind == "assistant":
        return _assistant_renderable(item, language)
    if item.kind in ("thinking", "tool_call", "tool_result", "approval") and item.collapsible:
        return trace_renderable(item, language)
    if item.kind == "tool_call":
        try:
            args = json.dumps(json.loads(item.tool_args), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            args = item.tool_args
        return Panel(Text(_truncate(args, 4_000, language)), title=f"{translate(language, 'ui.transcript.tool_call')} · {item.tool_name}", border_style="cyan")
    if item.kind == "tool_result":
        style = "green" if item.tool_ok else "red"
        return Panel(Text(_truncate(item.text, language=language)), title=f"{translate(language, 'ui.transcript.tool_result')} · {item.tool_name}", border_style=style)
    if item.kind == "approval":
        return Panel(Text(item.text or translate(language, "ui.approval.title")), title=f"{translate(language, 'ui.transcript.approval')} · {item.tool_name}", border_style="yellow")
    if item.kind == "context":
        return Panel(Text(item.text), title=translate(language, "ui.transcript.context"), border_style="blue")
    if item.kind == "help":
        return Panel(Text(_truncate(item.text, language=language)), title=translate(language, "ui.transcript.help"), border_style="cyan")
    if item.kind == "plan":
        plan = item.plan
        if plan is None:
            return Panel(Text(item.text), title=translate(language, "ui.transcript.plan"), border_style="magenta")
        if item.plan_review:
            return PlanReviewCard(item, language)
        lines = [translate(language, "ui.plan.goal", goal=plan.goal), translate(language, "ui.plan.rounds", count=len(plan.batches))]
        for batch_no, batch in enumerate(plan.batches, 1):
            lines.append(translate(language, "ui.plan.round", round=batch_no, tasks=', '.join(batch)))
            for task_id in batch:
                task = plan.task_by_id(task_id)
                if task is None:
                    continue
                lines.append(f"  [{task.status}] {task.id}  {task.title}")
                if not item.collapsed:
                    deps = translate(language, "ui.plan.dependencies", dependencies=', '.join(task.deps)) if task.deps else ""
                    lines.append(f"      {task.description}{deps}")
                    mode = getattr(task, "resource_scope_mode", "")
                    if mode:
                        lines.append(translate(language, "ui.plan.resource_mode", mode=mode))
                    for claim in getattr(task, "resource_claims", ()):
                        lines.append(translate(language, "ui.plan.resource_claim", access=claim.access, pattern=claim.pattern))
        lines.append("")
        lines.append(translate(language, "ui.plan.execute_detail_hint"))
        return Panel(Text("\n".join(lines)), title=translate(language, "ui.transcript.plan_review"), border_style="magenta")
    if item.kind == "error":
        return Panel(Text(item.text), title=translate(language, "ui.transcript.error"), border_style="red")
    return Text(item.text, style="dim")
