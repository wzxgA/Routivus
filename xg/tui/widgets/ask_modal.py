"""Ask-User 交互面板：展示问题+选项，支持选择/自定义输入，Esc 跳过。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from xg.ask.models import AskOption, AskRequest
from xg.tui.state import TuiState
from xg.tui.i18n import UiLanguage, normalize_language, translate


class AskModal(ModalScreen[TuiState]):
    """Legacy Ask-User modal kept for compatibility.

    The main Textual app now renders :class:`AskPanel` inline above the
    Composer. This class is no longer opened by ``XgTuiApp`` and can be
    removed after downstream integrations stop importing it.

    一次 ask_user 的作答面板。

    每个问题可键盘（数字 1..n）或点击选项选择；也允许在 CustomInput 里输入自由文本。
    Esc 跳过本次询问（fail-closed，不代选默认值）。
    """

    def __init__(self, ask: AskRequest, language: UiLanguage = "zh") -> None:
        super().__init__(id="ask-screen")
        self.ask = ask
        self.language = normalize_language(language)
        # 选项的索引 {field_key: {'opt': index, 'custom': bool}}
        self._selection: dict[str, dict] = {}
        self._focus_index: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-dialog"):
            source = translate(self.language, "ui.ask.source", origin=self.ask.origin) if self.ask.origin else ""
            title = translate(self.language, "ui.ask.confirm", source=source)
            yield Static(title)
            if self.ask.prompt:
                yield Static(self.ask.prompt)
            self._field_inputs: dict[str, Input] = {}
            with VerticalScroll(id="ask-fields"):
                for field in self.ask.fields:
                    options = field.options
                    with Vertical(classes="ask-field"):
                        required = translate(self.language, "ui.ask.required") if field.required else ""
                        yield Label(f"Q: {field.question}{required}")
                        if options:
                            for index, option in enumerate(options, start=1):
                                yield Button(
                                    f"{index}. {option.label}",
                                    id=f"ask-opt-{field.key}-{index}",
                                    classes="ask-option",
                                )
                        custom_input = Input(
                            placeholder=translate(
                                self.language,
                                "ui.ask.custom_placeholder" if field.allow_custom else "ui.ask.no_custom_placeholder",
                            ),
                            id=f"ask-input-{field.key}",
                        )
                        self._field_inputs[field.key] = custom_input
                        yield custom_input
            with Vertical(id="ask-actions"):
                yield Button(
                    "Submit (Enter)" if self.language == "en" else "提交 (Enter)",
                    id="ask-confirm", variant="primary",
                )
                yield Button(
                    "Skip (Esc)" if self.language == "en" else "跳过 (Esc)",
                    id="ask-skip", variant="error",
                )

    def on_mount(self) -> None:
        # 聚焦第一个可输入框，方便直接键入自定义答案
        if self.ask.fields:
            first = self.ask.fields[0]
            self._focus_index.setdefault(first.key, 0)
            self.query_one(f"#ask-input-{first.key}", Input).focus()

    def _selected_value(self, field) -> str | None:
        inputs = self._selection.get(field.key) or {}
        index = inputs.get("opt")
        if index is not None and field.options:
            option = field.options[index - 1]
            return option.value or option.label
        custom = self._field_inputs[field.key].value.strip()
        return custom or (field.default or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        widget_id = event.button.id or ""
        if widget_id == "ask-confirm":
            self._confirm()
        elif widget_id == "ask-skip":
            self._skip()
        elif widget_id.startswith("ask-opt-"):
            # Textual 8.x Button doesn't expose a ``data`` constructor
            # argument. The field key may contain hyphens, so split only
            # at the final separator before the option index.
            option_id = widget_id.removeprefix("ask-opt-")
            field_key, _, index_text = option_id.rpartition("-")
            if not field_key or not index_text.isdigit():
                return
            index = int(index_text)
            self._selection[field_key] = {"opt": index}
            self._focus_index[field_key] = index
            # 点亮当前选择
            for key in (f"ask-opt-{field_key}-{i}" for i in range(1, len(self._field_inputs) + 1)):
                pass
        elif widget_id == "ask-custom":
            pass

    async def _confirm(self) -> None:
        answers: dict[str, str] = {}
        missing_required = False
        for field in self.ask.fields:
            value = self._selected_value(field)
            if value is None and field.required:
                missing_required = True
                continue
            if value is not None:
                answers[field.key] = value
        if missing_required or (not answers and not self._has_any_custom()):
            pass
        await self.dismiss(answers)

    def _has_any_custom(self) -> bool:
        return any(inp.value.strip() for inp in self._field_inputs.values())

    def _skip(self) -> None:
        self.dismiss(None)

    BINDINGS = [("escape", "skip", "跳过本次询问")]

    def action_skip(self) -> None:
        self._skip()

    async def action_submit(self) -> None:
        await self._confirm()
