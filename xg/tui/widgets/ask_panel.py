"""Inline Ask-User panel rendered above the main Composer."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Static

from xg.ask.models import AskField, AskRequest
from xg.tui.messages import AskOptionSelected
from xg.tui.i18n import UiLanguage, normalize_language, translate


class AskOptionButton(Button):
    """A stable option button without Textual ``Button(data=...)`` usage."""

    def __init__(self, option_index: int) -> None:
        super().__init__("", id=f"ask-option-{option_index}", classes="ask-option")
        self.option_index = option_index


@dataclass
class AskViewState:
    request_id: str = ""
    field_index: int = 0
    selected_option: int | None = None
    answers: dict[str, str] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class AskSubmitResult:
    request_id: str
    complete: bool = False
    answers: dict[str, str] = field(default_factory=dict)
    error: str = ""


class AskPanel(Vertical):
    """Stable inline question card used by the main TUI.

    The panel owns only transient UI state. It never calls the controller or
    resolves the agent Future directly; the app translates the result into a
    ``SessionController.submit_ask_answer`` call.
    """

    MAX_OPTION_SLOTS = 20

    def __init__(self, **kwargs) -> None:
        super().__init__(id="ask-panel", classes="ask-panel", **kwargs)
        self.request: AskRequest | None = None
        self.language: UiLanguage = "en"
        self._view = AskViewState()
        self._kicker = Static("", id="ask-kicker")
        self._prompt = Static("", id="ask-prompt")
        self._progress = Static("", id="ask-progress")
        self._question = Static("", id="ask-question")
        self._hint = Static("", id="ask-hint")
        self._error = Static("", id="ask-error")
        self._buttons = [
            AskOptionButton(index)
            for index in range(1, self.MAX_OPTION_SLOTS + 1)
        ]
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._kicker
        yield self._prompt
        yield self._progress
        yield self._question
        with VerticalScroll(id="ask-options"):
            yield from self._buttons
        yield self._error
        yield self._hint

    def on_mount(self) -> None:
        self._sync_render()

    @property
    def current_field(self) -> AskField | None:
        if self.request is None or not self.request.fields:
            return None
        if not 0 <= self._view.field_index < len(self.request.fields):
            return None
        return self.request.fields[self._view.field_index]

    def update_request(self, request: AskRequest | None) -> None:
        """Update the stable panel without replacing its DOM children."""
        previous_id = self._view.request_id
        request_id = request.id if request is not None else ""
        if request_id != previous_id:
            self._view = AskViewState(request_id=request_id)
        self.request = request
        self.display = request is not None
        self._sync_render()

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)
        self._sync_render()

    def _sync_render(self) -> None:
        request = self.request
        if request is None:
            self.display = False
            return

        self.display = True
        source = translate(self.language, "ui.ask.source", origin=request.origin) if request.origin else ""
        self._kicker.update(translate(self.language, "ui.ask.confirm", source=source))
        self._prompt.update(request.prompt)
        self._prompt.display = bool(request.prompt)

        field = self.current_field
        if field is None:
            self._progress.update(translate(self.language, "ui.ask.more"))
            self._question.update(translate(self.language, "ui.ask.question"))
            options: tuple = ()
            allow_custom = True
        else:
            total = len(request.fields)
            self._progress.update(translate(self.language, "ui.ask.progress", current=self._view.field_index + 1, total=total, options=len(field.options)))
            required = translate(self.language, "ui.ask.required") if field.required else ""
            self._question.update(f"{field.question}{required}")
            options = field.options
            allow_custom = field.allow_custom

        for button in self._buttons:
            index = button.option_index
            if index <= len(options):
                option = options[index - 1]
                button.label = f"{index}. {option.label}"
                button.display = True
                button.set_class(index == self._view.selected_option, "selected")
            else:
                button.label = ""
                button.display = False
                button.remove_class("selected")

        if self._view.error:
            self._error.update(self._view.error)
            self._error.display = True
        else:
            self._error.update("")
            self._error.display = False

        if field is not None and not allow_custom and options:
            hint = translate(self.language, "ui.ask.hint.options_only")
        elif self._view.field_index + 1 < len(request.fields):
            hint = translate(self.language, "ui.ask.hint.next")
        else:
            hint = translate(self.language, "ui.ask.hint.submit")
        self._hint.update(hint)

    def select_option(self, option_index: int) -> bool:
        field = self.current_field
        if field is None or not 1 <= option_index <= len(field.options):
            return False
        self._view.selected_option = option_index
        self._view.error = ""
        self._sync_render()
        return True

    def move_selection(self, delta: int) -> bool:
        field = self.current_field
        if field is None or not field.options:
            return False
        count = len(field.options)
        current = self._view.selected_option
        next_index = 1 if current is None else ((current - 1 + delta) % count) + 1
        return self.select_option(next_index)

    def clear_selection_for_custom_input(self, text: str) -> None:
        if text and self._view.selected_option is not None:
            self._view.selected_option = None
            self._view.error = ""
            self._sync_render()

    def submit_text(self, text: str) -> AskSubmitResult:
        """Consume the current Composer text and advance one field."""
        request = self.request
        if request is None:
            return AskSubmitResult(request_id="", error=translate(self.language, "ui.ask.no_pending"))

        field = self.current_field
        if field is None:
            return AskSubmitResult(
                request_id=request.id,
                complete=True,
                answers=dict(self._view.answers),
            )

        raw = text.strip()
        value: str | None = None
        if raw:
            if raw.isdigit() and 1 <= int(raw) <= len(field.options):
                option = field.options[int(raw) - 1]
                value = option.value or option.label
                self._view.selected_option = int(raw)
            elif not field.allow_custom:
                return self._reject(translate(self.language, "ui.ask.options_only_error"))
            else:
                value = raw
        elif self._view.selected_option is not None:
            option = field.options[self._view.selected_option - 1]
            value = option.value or option.label
        elif field.required:
            return self._reject(translate(self.language, "ui.ask.required_error"))

        if value is not None:
            self._view.answers[field.key] = value
        else:
            self._view.answers.pop(field.key, None)

        if self._view.field_index + 1 < len(request.fields):
            self._view.field_index += 1
            self._view.selected_option = None
            self._view.error = ""
            self._sync_render()
            return AskSubmitResult(request_id=request.id)

        answers = dict(self._view.answers)
        return AskSubmitResult(request_id=request.id, complete=True, answers=answers)

    def _reject(self, message: str) -> AskSubmitResult:
        request_id = self.request.id if self.request is not None else ""
        self._view.error = message
        self._sync_render()
        return AskSubmitResult(request_id=request_id, error=message)

    def cancel(self) -> str:
        return self.request.id if self.request is not None else ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, AskOptionButton):
            self.post_message(AskOptionSelected(
                request_id=self._view.request_id,
                option_index=event.button.option_index,
            ))
            event.stop()
