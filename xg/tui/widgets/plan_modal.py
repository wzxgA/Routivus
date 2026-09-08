from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from xg.agent.plan import Plan, ReviewDecision
from xg.tui.i18n import UiLanguage, normalize_language, translate


class PlanModal(ModalScreen[str]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("enter", "execute", "Execute"), ("d", "details", "Details"), ("r", "replan", "Replan")]

    def __init__(self, plan: Plan, language: UiLanguage = "zh") -> None:
        super().__init__()
        self.plan = plan
        self.language = normalize_language(language)
        self._details = False

    def compose(self) -> ComposeResult:
        yield from self._content()

    def _content(self) -> list:
        lines = [translate(self.language, "ui.plan.goal", goal=self.plan.goal), translate(self.language, "ui.plan.rounds", count=len(self.plan.batches))]
        for round_no, batch in enumerate(self.plan.batches, 1):
            lines.append(translate(self.language, "ui.plan.round", round=round_no, tasks=', '.join(batch)))
            for task_id in batch:
                task = self.plan.task_by_id(task_id)
                if task is None:
                    continue
                lines.append(f"{task.id} [{task.status}] {task.title}")
                if self._details:
                    lines.append(f"  {task.description}")
        return [Vertical(Static("\n".join(lines)), Input(placeholder=translate(self.language, "ui.plan.replan_placeholder"), id="plan-feedback"), Button(translate(self.language, "ui.plan.execute"), id="execute", variant="success"), Button(translate(self.language, "ui.config.cancel"), id="cancel", variant="error"))]

    def action_details(self) -> None:
        self._details = not self._details
        self.refresh(recompose=True)

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def action_execute(self) -> None:
        self.dismiss("execute")

    def action_replan(self) -> None:
        self.query_one("#plan-feedback", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "plan-feedback" and event.value.strip():
            self.dismiss("replan:" + event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "cancel")
