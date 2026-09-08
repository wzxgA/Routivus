from __future__ import annotations

import asyncio

from textual.events import Key
from textual.widgets import Input

from xg.input_history import InputHistory
from xg.tui.i18n import UiLanguage, normalize_language, translate
from xg.tui.widgets.command_suggestions import CommandSuggestions


class Composer(Input):
    """Single-line Composer with bounded XG input history navigation."""

    BINDINGS = [("escape", "escape_input", "Clear input or cancel current action")]

    def __init__(self) -> None:
        super().__init__(placeholder=translate("en", "ui.composer.placeholder"), id="composer")
        self.language: UiLanguage = "en"
        self.suggestions_enabled = True
        self.input_history: InputHistory | None = None
        self.ask_request = None
        self.ask_mode = False

    def set_input_history(self, history: InputHistory | None) -> None:
        self.input_history = history

    def set_language(self, language: UiLanguage) -> None:
        self.language = normalize_language(language)
        if self.ask_mode:
            self.placeholder = translate(self.language, "ui.ask.placeholder")
        else:
            self.placeholder = translate(self.language, "ui.composer.placeholder")

    def record_submission(self, text: str, accepted: bool) -> None:
        if self.input_history is None:
            return
        if accepted:
            self.input_history.add(text)
        else:
            self.input_history.reset_cursor()

    def reset_history_cursor(self) -> None:
        if self.input_history is not None:
            self.input_history.reset_cursor()

    def set_ask_mode(self, ask) -> bool:
        """Switch the stable Composer between task and Ask-User input modes."""
        request_id = getattr(ask, "id", "") if ask is not None else ""
        previous_id = getattr(self.ask_request, "id", "") if self.ask_request is not None else ""
        changed = request_id != previous_id
        self.ask_request = ask
        self.ask_mode = ask is not None
        if self.ask_mode:
            if changed:
                self.value = ""
            self.placeholder = translate(self.language, "ui.ask.placeholder")
            self.add_class("ask-mode")
        else:
            if changed:
                self.value = ""
            self.placeholder = translate(self.language, "ui.composer.placeholder")
            self.remove_class("ask-mode")
        return changed

    def _history_blocked(self, app) -> bool:
        if self.ask_mode or getattr(app, "_replan_mode", False):
            return True
        controller = getattr(app, "controller", None)
        state = getattr(controller, "state", None)
        return bool(
            getattr(app, "_has_pending_plan", lambda: False)()
            or getattr(state, "pending_approval", None) is not None
            or getattr(state, "pending_confirmation", None) is not None
        )

    def _navigate_history(self, direction: str) -> None:
        if self.input_history is None:
            return
        suggestions = self._suggestions()
        if suggestions is not None:
            suggestions.close()
        value = (
            self.input_history.previous(self.value)
            if direction == "previous"
            else self.input_history.next()
        )
        self.value = value
        self.cursor_position = len(value)
        self.focus()

    def set_suggestions_enabled(self, enabled: bool) -> None:
        self.suggestions_enabled = enabled

    def _suggestions(self) -> CommandSuggestions | None:
        try:
            return self.app.query_one("#command-suggestions", CommandSuggestions)
        except Exception:
            # Input can receive an event while the app is mounting or
            # unmounting. In that short window there is no suggestion widget.
            return None

    def on_input_changed(self, event: Input.Changed) -> None:
        """Keep filtering local and leave submission to the App."""
        if event.input is not self or not self.suggestions_enabled:
            if event.input is self and self.ask_mode:
                self.app.handle_ask_text_changed(event.value)
            return
        if self.ask_mode:
            self.app.handle_ask_text_changed(event.value)
            return
        suggestions = self._suggestions()
        if suggestions is not None:
            suggestions.update_query(event.value, self.cursor_position)

    def on_key(self, event: Key) -> None:
        """Keep plan-review shortcuts working while the input stays enabled."""
        app = self.app
        if self.ask_mode:
            if event.key in ("up", "down"):
                handled = app.handle_ask_navigation(-1 if event.key == "up" else 1)
                if handled:
                    event.prevent_default()
                    event.stop()
                return
            if event.key.isdigit() and not self.value:
                if app.handle_ask_option_key(int(event.key)):
                    event.prevent_default()
                    event.stop()
                return
            return
        suggestions = self._suggestions()
        if suggestions is not None and suggestions.is_open:
            if event.key == "up":
                event.prevent_default()
                event.stop()
                suggestions.move_selection(-1)
                return
            if event.key == "down":
                event.prevent_default()
                event.stop()
                suggestions.move_selection(1)
                return
            if event.key == "tab":
                candidate = suggestions.selected_candidate()
                if candidate is not None:
                    event.prevent_default()
                    event.stop()
                    app.complete_command_suggestion(candidate)
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                suggestions.close()
                return

        has_pending_plan = getattr(app, "_has_pending_plan", lambda: False)()
        if not self._history_blocked(app):
            if event.key in ("up", "pageup"):
                event.prevent_default()
                event.stop()
                self._navigate_history("previous")
                return
            if event.key in ("down", "pagedown"):
                event.prevent_default()
                event.stop()
                self._navigate_history("next")
                return
        if not has_pending_plan or getattr(app, "_replan_mode", False):
            if (
                event.key == "d"
                and not self.value
                and getattr(app, "_has_detail_toggle_target", lambda: False)()
            ):
                event.prevent_default()
                event.stop()
                app.action_plan_details()
            return
        if event.key == "r":
            event.prevent_default()
            event.stop()
            app.action_plan_replan()
        elif event.key == "d":
            event.prevent_default()
            event.stop()
            app.action_plan_details()
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            app.action_plan_execute()
        elif event.key == "escape":
            event.prevent_default()
            event.stop()
            asyncio.create_task(app.action_escape())

    async def action_escape_input(self) -> None:
        # Input consumes Escape before it bubbles to the App. Delegate to the
        # app so idle, running, plan-review and replan states share one policy.
        await self.app.action_escape()
