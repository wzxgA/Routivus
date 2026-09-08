from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from xg.tui.state import ConfirmationRequest
from xg.tui.i18n import UiLanguage, normalize_language, translate


class ConfirmModal(ModalScreen[str]):
    BINDINGS = [("escape", "cancel", "取消"), ("enter", "confirm", "确认"), ("y", "confirm", "确认")]

    def __init__(self, request: ConfirmationRequest, language: UiLanguage = "zh") -> None:
        super().__init__()
        self.request = request
        self.language = normalize_language(language)

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(f"{self.request.title}\n\n{self.request.body}")
            yield Button("Confirm (y/Enter)" if self.language == "en" else "确认 (y/Enter)", id="confirm", variant="warning")
            yield Button(translate(self.language, "ui.config.cancel"), id="cancel")

    def action_confirm(self) -> None:
        self.dismiss("confirm")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "cancel")
