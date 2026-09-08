"""Inline action cards used for all TUI confirmations.

Cards are read-only text: context, risk and the input protocol.  Decisions
are always typed into the main Composer, so no Button or inner Input lives
here.
"""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from xg.tui.state import ApprovalRequest, ConfirmationRequest
from xg.tui.i18n import UiLanguage, normalize_language, translate

APPROVAL_HINT = "ui.approval.hint"
CONFIRMATION_HINTS = {
    "init": "ui.confirm.write_hint",
    "memory_clear": "ui.confirm.clear_hint",
}


class InlineConfirmationCard(Vertical):
    def __init__(self, request: ConfirmationRequest, language: UiLanguage = "zh") -> None:
        # Per-kind class lets the theme give each confirmation its own
        # border color while sharing one simple layout.  No fixed widget id:
        # TranscriptView keeps this card stable and updates its content in
        # place, so no nested input or duplicate child id is required.
        super().__init__(
            classes=f"inline-action-card inline-confirmation-card confirmation-{request.kind}",
        )
        self.request = request
        self.language = normalize_language(language)
        # The Static is created eagerly: compose() runs asynchronously, so
        # updates must not depend on the card being mounted already.
        hint = translate(self.language, CONFIRMATION_HINTS.get(self.request.kind, "ui.confirm.write_hint"))
        self._text = Static(f"{self.request.title}\n\n{self.request.body}\n\n{hint}")

    def compose(self) -> ComposeResult:
        yield self._text

    def update_request(self, request: ConfirmationRequest) -> None:
        """Update the stable card without replacing its DOM node."""
        self.request = request
        hint = translate(self.language, CONFIRMATION_HINTS.get(self.request.kind, "ui.confirm.write_hint"))
        self._text.update(f"{self.request.title}\n\n{self.request.body}\n\n{hint}")

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)
        self.update_request(self.request)


class InlineApprovalCard(Vertical):
    def __init__(self, request: ApprovalRequest, language: UiLanguage = "zh") -> None:
        super().__init__(classes="inline-action-card inline-approval-card")
        self.request = request
        self.language = normalize_language(language)
        self._mode = ""
        self._modified_args: dict | None = None
        self._text = Static(self._render_text(), classes="inline-approval-text")

    def compose(self) -> ComposeResult:
        yield self._text

    def update_request(self, request: ApprovalRequest) -> None:
        """Update request data while preserving the card and its mode."""
        self.request = request
        self._text.update(self._render_text())

    def set_mode(self, mode: str = "", modified_args: dict | None = None) -> None:
        """Switch the card text; empty mode means waiting for the decision."""
        self._mode = mode
        self._modified_args = modified_args
        self._text.update(self._render_text())

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)
        self._text.update(self._render_text())

    def _render_text(self) -> str:
        if self._mode == "approval_edit":
            return (
                f"{translate(self.language, 'ui.approval.title')}：{self.request.tool_name}\n\n"
                f"{translate(self.language, 'ui.approval.edit')}\n"
                f"{translate(self.language, 'ui.approval.guard')}\n"
                f"{translate(self.language, 'ui.approval.cancel_edit')}"
            )
        if self._mode == "approval_confirm_modified":
            args = json.dumps(self._modified_args or {}, ensure_ascii=False, indent=2)
            return (
                f"{translate(self.language, 'ui.approval.title')}：{self.request.tool_name}\n\n"
                f"{translate(self.language, 'ui.approval.modified')}\n{args}\n\n"
                f"{translate(self.language, 'ui.approval.execute_hint')}"
            )
        args = json.dumps(self.request.args, ensure_ascii=False, indent=2)
        return (
            f"{translate(self.language, 'ui.approval.title')}\n"
            f"{translate(self.language, 'ui.approval.tool', tool=self.request.tool_name)}\n"
            f"{translate(self.language, 'ui.approval.level', level=self.request.level)}\n"
            f"{translate(self.language, 'ui.approval.args')}\n{args}\n\n"
            f"{translate(self.language, APPROVAL_HINT)}"
        )
