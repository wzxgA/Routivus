"""Plan review flowchart renderables for the Textual transcript."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.panel import Panel
from rich.text import Text

from xg.agent.plan import Plan, PlanTask
from xg.agent.team import TeamPlan
from xg.tui.diagrams.model import FlowchartEdge, FlowchartModel, FlowchartNode
from xg.tui.diagrams.renderer import render_flowchart
from xg.tui.state import TranscriptItem
from xg.tui.i18n import UiLanguage, normalize_language, translate


@dataclass(frozen=True)
class PlanFlowchartData:
    model: FlowchartModel
    rank_by_node: dict[str, int]
    ordered_ids: tuple[str, ...]


def plan_to_flowchart(plan: Plan | TeamPlan, language: UiLanguage = "zh") -> PlanFlowchartData:
    """Adapt an already validated Plan DAG to the text flowchart model."""
    tasks = {task.id: task for task in plan.tasks}
    ordered_ids: list[str] = []
    rank_by_node: dict[str, int] = {}
    for batch_no, batch in enumerate(plan.batches):
        for task_id in batch:
            if task_id in tasks and task_id not in ordered_ids:
                ordered_ids.append(task_id)
                rank_by_node[task_id] = batch_no
    # Defensive fallback for a manually-created Plan whose batches are stale.
    for task in plan.tasks:
        if task.id not in ordered_ids:
            ordered_ids.append(task.id)
            rank_by_node[task.id] = len(plan.batches)

    nodes = [
        FlowchartNode(id=task_id, label=f"{task_id} {tasks[task_id].title}")
        for task_id in ordered_ids
    ]
    known = set(tasks)
    edges = [
        FlowchartEdge(source=dependency, target=task.id)
        for task in plan.tasks
        for dependency in task.deps
        if dependency in known and task.id in known
    ]
    warnings = []
    missing = [
        translate(language, "ui.plan.missing_dependency", task=task.id, dependency=dependency)
        for task in plan.tasks
        for dependency in task.deps
        if dependency not in known
    ]
    warnings.extend(missing)
    return PlanFlowchartData(
        model=FlowchartModel(direction="TD", nodes=nodes, edges=edges, warnings=warnings),
        rank_by_node=rank_by_node,
        ordered_ids=tuple(ordered_ids),
    )


def _task_title_text(task: PlanTask) -> Text:
    """Render a task title with a consistent visual hierarchy."""
    line = Text(style="bold")
    line.append(task.id, style="cyan bold")
    line.append(f" {task.title}", style="bold")
    return line


def _task_summary_renderable(plan: Plan | TeamPlan, ordered_ids: tuple[str, ...], detailed: bool, language: UiLanguage = "zh") -> Group:
    """Render the task heading, keyboard hint, and task content separately."""
    tasks = {task.id: task for task in plan.tasks}
    renderables: list[object] = [
        Text(translate(language, "ui.plan.task_detail" if detailed else "ui.plan.task_summary")),
        Text(translate(language, "ui.plan.hide_detail" if detailed else "ui.plan.show_detail"), style="dim"),
    ]
    for task_id in ordered_ids:
        task = tasks.get(task_id)
        if task is None:
            continue
        renderables.append(_task_title_text(task))
        if detailed:
            deps = ", ".join(task.deps) if task.deps else translate(language, "ui.config.empty")
            renderables.extend([
                Text(translate(language, "ui.plan.description", description=task.description or translate(language, "ui.config.empty")), style="dim"),
                Text(translate(language, "ui.plan.dependencies", dependencies=deps), style="dim"),
            ])
            tools = getattr(task, "allowed_tools", ())
            if tools:
                renderables.append(Text(translate(language, "ui.plan.tools", tools=', '.join(tools)), style="dim"))
            for warning in getattr(task, "tool_warnings", ()):
                renderables.append(Text(translate(language, "ui.plan.tool_hint", warning=warning), style="yellow"))
            mode = getattr(task, "resource_scope_mode", "")
            if mode:
                renderables.append(Text(translate(language, "ui.plan.resource_mode", mode=mode), style="dim"))
            for claim in getattr(task, "resource_claims", ()):
                renderables.append(
                    Text(translate(language, "ui.plan.resource_claim", access=claim.access, pattern=claim.pattern), style="dim")
                )
    return Group(*renderables)


class PlanReviewCard:
    """Static review card: flowchart plus summary/details below it."""

    def __init__(self, item: TranscriptItem, language: UiLanguage = "zh") -> None:
        self.item = item
        self.language: UiLanguage = normalize_language(language)

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        plan = self.item.plan
        # /plan uses Plan while /team uses TeamPlan. Both expose the same
        # validated task/batch shape needed by this read-only renderer.
        if not isinstance(plan, (Plan, TeamPlan)):
            yield Panel(Text(self.item.text or translate(self.language, "ui.plan.unavailable")), title=translate(self.language, "ui.transcript.plan_review"), border_style="magenta")
            return
        data = plan_to_flowchart(plan, self.language)
        chart = render_flowchart(
            data.model,
            width=max(1, options.max_width - 8),
            rank_by_node=data.rank_by_node,
        )
        parts: list[object] = [
            Text(f"{translate(self.language, 'ui.plan.goal', goal=plan.goal)}\n{translate(self.language, 'ui.plan.rounds', count=len(plan.batches))}", style="bold"),
        ]
        # Skip the flow-chart panel entirely when rendering falls back to
        # structured text: the panel would just repeat the task IDs already
        # listed below, and the warning inside a cyan border looks noisy.
        if chart.mode != "structured":
            chart_parts: list[object] = [Text(chart.text, no_wrap=True, overflow="crop")]
            if chart.warnings:
                chart_parts.append(Text("\n\n⚠ " + "；".join(chart.warnings)))
            parts.append(Panel(Group(*chart_parts), title=translate(self.language, "ui.plan.flow"), border_style="cyan"))
        elif chart.warnings:
            parts.append(Text("⚠ " + "；".join(chart.warnings), style="dim"))
        batch_lines = [
            translate(self.language, "ui.plan.round", round=batch_no, tasks=', '.join(batch))
            for batch_no, batch in enumerate(plan.batches, 1)
        ]
        parts.append(Text("\n".join(batch_lines), style="dim"))
        parts.append(_task_summary_renderable(plan, data.ordered_ids, not self.item.collapsed, self.language))
        parts.append(Text("\n" + translate(self.language, "ui.plan.execute_hint"), style="dim"))
        yield Panel(Group(*parts), title=translate(self.language, "ui.transcript.plan_review"), border_style="magenta")
