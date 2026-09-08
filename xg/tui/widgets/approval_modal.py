from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from xg.safety.hitl import ApprovalDecision
from xg.tui.state import ApprovalRequest
from xg.tui.i18n import UiLanguage, normalize_language, translate


class ApprovalModal(ModalScreen[str]):
    BINDINGS = [("escape", "reject", "Reject"), ("enter", "approve", "Approve"), ("y", "approve", "Approve"), ("a", "allow_all", "Allow all"), ("r", "reject", "Reject"), ("s", "skip", "Skip"), ("e", "edit", "Edit parameters")]

    def __init__(self, request: ApprovalRequest, language: UiLanguage = "zh") -> None:
        super().__init__()
        self.request = request
        self.language = normalize_language(language)

    def compose(self) -> ComposeResult:
        args = json.dumps(self.request.args, ensure_ascii=False, indent=2)
        with Vertical(id="approval-dialog"):
            yield Static(f"{translate(self.language, 'ui.approval.title')}：{self.request.tool_name}\n{translate(self.language, 'ui.approval.level', level=self.request.level)}\n\n{args}")
            yield Input(placeholder=translate(self.language, "ui.approval.edit_placeholder"), id="approval-json")
            yield Button("Approve (Enter/y)" if self.language == "en" else "批准 (Enter/y)", id="approve", variant="success")
            yield Button("Allow all for this session (a)" if self.language == "en" else "本会话全部放行 (a)", id="allow-all")
            yield Button("Reject (r/Esc)" if self.language == "en" else "拒绝 (r/Esc)", id="reject", variant="error")

    def _close(self, value: str) -> None:
        self.dismiss(value)

    def action_approve(self) -> None:
        self._close("approve")

    def action_allow_all(self) -> None:
        self._close("allow_all")

    def action_reject(self) -> None:
        self._close("reject")

    def action_skip(self) -> None:
        self._close("skip")

    def action_edit(self) -> None:
        self.query_one("#approval-json", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "approval-json":
            return
        try:
            json.loads(event.value)
        except json.JSONDecodeError:
            event.input.value = ""
            return
        self._close("modify:" + event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._close(event.button.id or "reject")
