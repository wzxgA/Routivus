"""统一配置面板（TUI，Ctrl+T）：Provider 与 SmartRouter 同级展示。

仅于 TUI 运行时 import。Provider 列表与 SmartRouter 四档配置在同一个
Modal 内左右分栏平级呈现；编辑/新建仍走各自的 Form Modal，开关状态变更
即时同步顶部 Header。动作落 :class:`provider_service.ProviderConfigService`
与 :class:`smart_router_service.SmartRouterConfigService`。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from xg.config.provider_service import ProviderConfigService
from xg.config.smart_router_service import SmartRouterConfigService
from xg.tui.widgets.provider_form import ProviderForm
from xg.tui.widgets.smart_router_form import SmartRouterForm
from xg.tui.i18n import UiLanguage, normalize_language, translate


class ConfigScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, manager, settings=None) -> None:
        super().__init__()
        self.manager = manager
        self.settings = settings
        self.language: UiLanguage = normalize_language(getattr(settings, "ui_language", "en"))
        self.provider_service = ProviderConfigService(manager, settings, language=self.language)
        self.router_service = SmartRouterConfigService(manager, settings, language=self.language)
        self._rows: list[dict] = []
        self._sel = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="config-dialog"):
            yield Static(translate(self.language, "ui.config.title"), id="cfg-title")
            with Vertical(id="cfg-body"):
                with Vertical(id="provider-section"):
                    yield Static(translate(self.language, "ui.config.provider_section"), id="cfg-sec-title")
                    yield Static(translate(self.language, "ui.config.provider_hint"), id="provider-hint")
                    yield OptionList(id="provider-list")
                    yield Static("", id="provider-detail")
                    with Horizontal(id="provider-actions"):
                        yield Button(translate(self.language, "ui.config.add"), id="add", variant="primary")
                        yield Button(translate(self.language, "ui.config.edit"), id="edit")
                        yield Button(translate(self.language, "ui.config.set_base"), id="switch")
                        yield Button(translate(self.language, "ui.config.remove"), id="remove", variant="error")
                        yield Button(translate(self.language, "ui.config.write_key"), id="key")
                with Vertical(id="sr-section"):
                    yield Static(translate(self.language, "ui.config.router_section"), id="cfg-sec-title")
                    yield Static(self._render_router(), id="sr-body")
                    with Horizontal(id="sr-actions"):
                        yield Button(translate(self.language, "ui.config.toggle"), id="toggle")
                        yield Button(translate(self.language, "ui.config.edit_tier"), id="tier", variant="primary")
                        yield Button(translate(self.language, "ui.config.refresh"), id="refresh")
                        yield Button(translate(self.language, "ui.config.close"), id="close")

    def _provider_label(self, row: dict) -> str:
        return (
            f"{row['name']:<12}"
            f"{row['default_model'][:16]:<16}"
            f"{'✓' if row['has_key'] else '✗':^5}"
            f"{'●' if row['is_base'] else ' ':^6}"
            f"{row['layer']}"
        )

    def _rebuild_provider_list(self, ol: OptionList) -> None:
        self._rows = self.provider_service.list()
        ol.clear_options()
        if not self._rows:
            self._sel = 0
            return
        for row in self._rows:
            ol.add_option(Option(self._provider_label(row), id=row["name"]))
        if self._sel >= len(self._rows):
            self._sel = 0
        ol.highlighted = self._sel

    async def on_mount(self) -> None:
        self._rebuild_provider_list(self.query_one("#provider-list", OptionList))
        self.query_one("#sr-body", Static).update(self._render_router())
        self._update_detail()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._sel = event.option_index
        self._update_detail()

    def _update_detail(self) -> None:
        """展示当前选中 provider 的详情（等价 /provider show，key 脱敏）。"""
        if not self._rows:
            detail = translate(self.language, "ui.config.no_provider")
        else:
            name = self._rows[self._sel]["name"]
            info = self.provider_service.get(name)
            if info is None:
                detail = translate(self.language, "ui.config.unknown_provider", name=name)
            else:
                lines = [
                    f"{info['name']}{'  ●base' if info['is_base'] else ''}  [{info['layer']} "
                    f"{translate(self.language, 'ui.config.layer')}]",
                    f"api_base     : {info['api_base']}",
                    f"default_model: {info['default_model']}",
                ]
                if info.get("display_name"):
                    lines.insert(1, f"display_name : {info['display_name']}")
                models = info.get("models") or []
                lines.append(f"models({len(models)})    : {', '.join(models) if models else translate(self.language, 'ui.config.empty')}")
                lines.append(
                    f"api_key      : {info['api_key_masked'] if info['has_key'] else translate(self.language, 'ui.config.not_configured')}"
                )
                refs = self.provider_service.referenced_by(name)
                if refs:
                    lines.append(
                        f"{translate(self.language, 'ui.config.references')}: {', '.join(refs)}"
                    )
                detail = "\n".join(lines)
        self.query_one("#provider-detail", Static).update(detail)

    def _render_router(self) -> str:
        cfg = self.router_service.get()
        enabled = bool(cfg.get("enabled", False))
        rows = self.router_service.list_tiers()
        lines = [translate(self.language, "ui.config.enabled" if enabled else "ui.config.disabled"),
                 "TIER       PROVIDER       MODEL"]
        for row in rows:
            if row["configured"]:
                lines.append(f"{row['name']:<10}{row['provider']:<14}{row['model']}")
            else:
                lines.append(f"{row['name']:<10}{translate(self.language, 'ui.config.fallback_active'):<14}-")
        return "\n".join(lines)

    def _refresh(self) -> None:
        self._rebuild_provider_list(self.query_one("#provider-list", OptionList))
        self.query_one("#sr-body", Static).update(self._render_router())
        self._update_detail()

    def _sync_header(self) -> None:
        """开关 / 档位变更后即时重建顶部 Header 路由行。"""
        controller = getattr(self.app, "controller", None)
        if controller is not None:
            controller._sync_smart_router_snapshot()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "close":
            self.dismiss(None)
            return
        if bid == "refresh":
            self._refresh()
            return
        if bid == "add":
            return self.app.push_screen(
                ProviderForm(self.provider_service, mode="add", language=self.language),
                callback=self._after_form,
            )
        if bid == "toggle":
            cfg = self.router_service.get()
            on = not bool(cfg.get("enabled", False))
            self.notify(self.router_service.set_enabled(on).message)
            self._refresh()
            self._sync_header()
            return
        if bid == "tier":
            return self.app.push_screen(
                SmartRouterForm(self.router_service, language=self.language), callback=self._after_form
            )
        if not self._rows:
            self.notify(translate(self.language, "ui.config.no_provider"), severity="warning")
            return
        if bid == "edit":
            return self.app.push_screen(
                ProviderForm(self.provider_service, name=self._rows[self._sel]["name"], mode="edit", language=self.language),
                callback=self._after_form,
            )
        if bid == "switch":
            self.notify(self.provider_service.switch(self._rows[self._sel]["name"]).message)
            self._refresh()
            return
        if bid == "remove":
            result = self.provider_service.remove(self._rows[self._sel]["name"], yes=True)
            self.notify(result.message, severity="error" if not result.ok else "information")
            self._refresh()
            return
        if bid == "key":
            return self.app.push_screen(
                ProviderForm(self.provider_service, name=self._rows[self._sel]["name"], mode="key", language=self.language),
                callback=self._after_form,
            )

    def _after_form(self, _result: object) -> None:
        self._refresh()
        self._sync_header()


def open_config_screen(app, manager, settings=None) -> None:
    """外部调用：打开统一配置面板（Ctrl+T）。"""
    app.push_screen(ConfigScreen(manager, settings))
