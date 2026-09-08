"""Provider 新增/编辑/写 Key 表单 Modal（TUI）。

仅于 TUI 运行时 import。字段含即时校验；API Key 为密码型输入，
改写前经 :meth:`ProviderConfigService.set_api_key` 的覆盖确认与脱敏。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from xg.config.provider_service import (
    OpResult,
    ProviderConfigService,
    validate_api_base,
    validate_model,
    validate_name,
)
from xg.tui.i18n import UiLanguage, normalize_language, translate


class ProviderForm(ModalScreen[None]):
    """mode: ``add`` / ``edit`` / ``key``。"""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, service: ProviderConfigService, *, mode: str, name: str | None = None, language: UiLanguage = "zh") -> None:
        super().__init__()
        self.service = service
        self.language = normalize_language(language)
        self.mode = mode
        # 用独立属性承载目标 provider 名，避开 ModalScreen.name 只读属性
        self._target = name
        self._row = service.get(name) if name else None

    def compose(self) -> ComposeResult:
        title = {
            "add": translate(self.language, "ui.config.add") + " Provider",
            "edit": translate(self.language, "ui.config.edit") + " Provider",
            "key": translate(self.language, "ui.config.write_key"),
        }[self.mode]
        with Vertical(id="provider-form"):
            yield Static(title, id="provider-form-title")
            if self.mode in ("add", "edit"):
                yield Static(translate(self.language, "ui.config.provider_name"), id="f-name-label")
                yield Input(
                    value=self._target or "", id="name",
                    placeholder=translate(self.language, "ui.config.name_hint"),
                    disabled=self.mode == "edit",
                )
            if self.mode == "add":
                yield Static("api_base", id="f-base-label")
                yield Input(value=(self._row or {}).get("api_base", ""), id="api_base",
                            placeholder="https://gateway.my.com/v1")
                yield Static("default_model", id="f-model-label")
                yield Input(value=(self._row or {}).get("default_model", ""), id="default_model")
                yield Static(translate(self.language, "ui.config.display_name_optional"), id="f-label-label")
                yield Input(value=(self._row or {}).get("display_name", ""), id="display_name")
                yield Static(translate(self.language, "ui.config.api_key_optional"), id="f-key-label")
                yield Input(value="", id="api_key", password=True, placeholder="XG_{NAME}_API_KEY")
            elif self.mode == "edit":
                yield Static("api_base", id="f-base-label")
                yield Input(value=(self._row or {}).get("api_base", ""), id="api_base")
                yield Static("default_model", id="f-model-label")
                yield Input(value=(self._row or {}).get("default_model", ""), id="default_model")
                yield Static(translate(self.language, "ui.config.models_hint"), id="f-models-label")
                yield Input(value=", ".join((self._row or {}).get("models") or []), id="models")
                yield Static(translate(self.language, "ui.config.display_name_optional"), id="f-label-label")
                yield Input(value=(self._row or {}).get("display_name", ""), id="display_name")
            elif self.mode == "key":
                yield Static(f"API Key ({self._target})", id="f-key-label")
                yield Input(value="", id="api_key", password=True, placeholder=f"XG_{self._target.upper()}_API_KEY")
            with Horizontal(id="provider-form-actions"):
                yield Button(translate(self.language, "ui.config.save"), id="save", variant="primary")
                yield Button(translate(self.language, "ui.config.cancel"), id="cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            self._save()
            return
        # Enter 在 Input 上触发同一保存逻辑
        if event.button.id == "" and self.mode in ("add", "edit", "key"):
            self._save()

    def _sync_models(self, name: str, raw: str, current: list[str]) -> None:
        """把编辑表单的模型串（逗号分隔）覆盖式同步到 provider 模型列表。"""
        seen: set[str] = set()
        desired = []
        for m in raw.split(","):
            m = m.strip()
            if m and m not in seen:
                seen.add(m)
                desired.append(m)
        for m in [x for x in current if x not in desired]:
            self.service.remove_model(name, m)
        for m in desired:
            self.service.add_model(name, m)

    def _save(self) -> None:
        if self.mode == "add":
            name = self.query_one("#name", Input).value.strip()
            base = self.query_one("#api_base", Input).value.strip()
            model = self.query_one("#default_model", Input).value.strip()
            label = self.query_one("#display_name", Input).value.strip() or None
            key = self.query_one("#api_key", Input).value.strip()
            err = validate_name(name, self.language) or validate_api_base(base, self.language)
            if err:
                return self.notify(err, severity="error")
            result = self.service.add(name, base, model, display_name=label)
            if result.ok and key:
                key_result = self.service.set_api_key(name, key, overwrite=True, yes=True)
                result = OpResult(
                    True,
                    f"{result.message}；{key_result.message}",
                ) if key_result.ok else key_result
        elif self.mode == "edit":
            base = self.query_one("#api_base", Input).value.strip()
            model = self.query_one("#default_model", Input).value.strip()
            label = self.query_one("#display_name", Input).value.strip() or None
            err = validate_api_base(base, self.language) or validate_model(model, self.language)
            if not model:
                err = err or translate(self.language, "ui.config.required_model")
            if err:
                return self.notify(err, severity="error")
            result = self.service.update(
                self._target, {"api_base": base, "default_model": model, "display_name": label}
            )
            if result.ok:
                self._sync_models(
                    self._target,
                    self.query_one("#models", Input).value,
                    list((self._row or {}).get("models") or []),
                )
        else:  # key
            key = self.query_one("#api_key", Input).value.strip()
            result = self.service.set_api_key(self._target, key, overwrite=True, yes=True)
        self.notify(result.message, severity="error" if not result.ok else "information")
        if result.ok:
            self.dismiss(None)
