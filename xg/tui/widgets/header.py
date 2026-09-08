from __future__ import annotations

import os

from rich.text import Text
from textual.widgets import Static

from xg.tui.state import SmartRouterSnapshot, SmartRouterTierSnapshot, TuiState
from xg.tui.i18n import UiLanguage, normalize_language, translate, ui_status


def _compact(value: int) -> str:
    if value <= 0:
        return "-"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}m".replace(".0m", "m")


def _watermelon_mark() -> str:
    """Return the brand mark with a safe ASCII fallback for dumb terminals."""
    return "[XG CLI]" if os.environ.get("TERM", "").lower() == "dumb" else "🍉 XG CLI"


def _status_label(phase: str, language: UiLanguage = "en") -> str:
    return {
        "idle": ui_status(language, "idle"),
        "running": ui_status(language, "working"),
        "awaiting_approval": translate(language, "status.waiting_approval"),
        "awaiting_plan_review": ui_status(language, "plan review"),
        "awaiting_team_input": translate(language, "status.waiting_team_input"),
        "error": ui_status(language, "error"),
    }.get(phase, ui_status(language, phase))


def _tier_segment(tier: SmartRouterTierSnapshot) -> Text:
    """Render one tier entry on the routing row.

    当前档 bold reverse；未配置档 dim + ``model (x)``；其余档 dim。
    """
    label = f"{tier.tier}: {tier.model}"
    if not tier.configured:
        label += " (x)"
    if tier.is_active:
        return Text(label, style="bold reverse")
    return Text(label, style="dim")


def _routing_row(snapshot: SmartRouterSnapshot) -> Text:
    """Assemble the four-tier routing row (only when enabled)."""
    row = Text()
    for index, tier in enumerate(snapshot.tiers):
        if index:
            row.append("  ")
        row.append(_tier_segment(tier))
    return row


class HeaderBar(Static):
    def __init__(self, *args, **kwargs) -> None:
        # The ASCII fallback is rendered literally; otherwise Textual markup
        # would treat the square brackets in ``[XG]`` as a tag.
        kwargs.setdefault("markup", False)
        super().__init__(*args, **kwargs)

    def update_state(self, state: TuiState) -> None:
        language = normalize_language(state.ui_language)
        status = _status_label(state.phase, language)
        inspector = state.inspector
        usage = inspector.usage
        level = "usage-normal"
        highest_ratio = max(usage.window_ratio, usage.budget_usage_ratio)
        if highest_ratio > 1.0:
            level = "usage-error"
        elif highest_ratio >= 0.7:
            level = "usage-warning"
        for class_name in ("usage-normal", "usage-warning", "usage-error"):
            self.remove_class(class_name)
        self.add_class(level)
        available = usage.estimated_prompt_tokens > 0 and usage.context_window > 0
        context = (
            f"{_compact(usage.estimated_prompt_tokens)}/{_compact(usage.context_window)}"
            if available else "-/-"
        )
        percentage = f"{usage.window_ratio * 100:.1f}%" if available else "-"
        provider_model = f"{inspector.provider}/{inspector.model}".strip("/") or translate(language, "ui.header.unavailable")
        hitl_enabled = inspector.safety.hitl_enabled if inspector.safety else inspector.hitl_enabled
        queue = f"  ·  {translate(language, 'ui.header.queue', count=len(state.queue))}" if state.queue else ""

        # Textual terminals cannot reliably display an SVG/PNG in the normal
        # text pipeline. The watermelon emoji is the color-capable mark, with
        # [XG] retained for TERM=dumb and other low-capability environments.
        mark = _watermelon_mark()
        # This is intentionally a welcome/brand block rather than a one-line
        # status bar.  Keeping the runtime fields in the same widget preserves
        # the existing state-update contract while giving the left column a
        # useful visual hierarchy.
        text = Text()
        text.append(mark, style="bold bright_green")
        text.append("\n")
        text.append(provider_model, style="bold")
        text.append("\n")
        # SmartRouter routing row (phase-02): rendered only when enabled so the
        # off-state output stays byte-identical to the pre-feature Header.
        if inspector.smart_router.enabled and inspector.smart_router.tiers:
            text.append(_routing_row(inspector.smart_router))
            text.append("\n")
        status_style = "red" if state.phase == "error" else "yellow" if state.phase != "idle" else "dim"
        text.append(status, style=status_style)
        text.append("  ·  ")
        text.append(f"{translate(language, 'ui.header.context')} {context} · {percentage}")
        text.append("\n")
        text.append(translate(language, "ui.header.hitl_on" if hitl_enabled else "ui.header.hitl_off"), style="yellow" if not hitl_enabled else "dim")
        text.append(queue, style="cyan")
        # Static.update(Rich Text) needs an active Textual app console in
        # Textual 8.x. Keep standalone render tests and lightweight adapters
        # usable by falling back to the plain representation when unmounted.
        self.update(text if self.is_mounted else text.plain)
