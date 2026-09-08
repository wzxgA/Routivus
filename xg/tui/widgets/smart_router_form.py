"""SmartRouter 档位表单 Modal（TUI）。

仅于 TUI 运行时 import。选择档位 + 填 provider；model 可选。
provider 留空 = 清空该档位（回落到手动 active）。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from xg.config.provider_service import validate_name
from xg.config.smart_router_service import SmartRouterConfigService
from xg.tui.i18n import UiLanguage, normalize_language, translate

_TIER_ORDER = ("Basic", "Enhanced", "Superior", "Ultimate")
_TIER_LABELS = {
    "Basic": "ui.tier.basic",
    "Enhanced": "ui.tier.enhanced",
    "Superior": "ui.tier.superior",
    "Ultimate": "ui.tier.ultimate",
}


class SmartRouterForm(ModalScreen[None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, service: SmartRouterConfigService, *, tier: str | None = None, language: UiLanguage = "zh") -> None:
        super().__init__()
        self.service = service
        self.language = normalize_language(language)
        self.configs = {
            r["name"]: {"provider": r.get("provider", ""), "model": r.get("model", "")}
            for r in service.list_tiers()
        }
        # 默认定位：显式指定档位 > 首个已配置档位 > Basic
        self.tier = tier
        if tier is None or tier not in _TIER_ORDER:
            configured = next(
                (t for t in _TIER_ORDER if self.configs[t]["provider"]), None
            )
            self.tier = configured or "Basic"

    def compose(self) -> ComposeResult:
        cur = self.configs.get(self.tier, {"provider": "", "model": ""})
        with Vertical(id="tier-form"):
            yield Static(translate(self.language, "ui.config.tier_title"), id="tier-form-title")
            yield Static(translate(self.language, "ui.config.tier"), id="t-tier-label")
            yield Select(
                ((translate(self.language, _TIER_LABELS.get(t, t)), t) for t in _TIER_ORDER),
                value=self.tier,
                allow_blank=False,
                id="tier",
            )
            yield Static(translate(self.language, "ui.config.provider_empty"), id="t-provider-label")
            yield Input(
                value=cur["provider"], id="provider",
                placeholder=translate(self.language, "ui.config.provider_placeholder")
            )
            yield Static(translate(self.language, "ui.config.model_optional"), id="t-model-label")
            yield Input(
                value=cur["model"], id="model",
                placeholder=translate(self.language, "ui.config.model_placeholder"),
            )
            with Horizontal(id="tier-form-actions"):
                yield Button(translate(self.language, "ui.config.save"), id="save", variant="primary")
                yield Button(translate(self.language, "ui.config.cancel"), id="cancel")

    def on_select_changed(self, event: Select.Changed) -> None:
        """切换档位时联动加载对应 provider/model 配置。"""
        tier = str(event.value or self.tier)
        cur = self.configs.get(tier, {"provider": "", "model": ""})
        self.query_one("#provider", Input).value = cur["provider"]
        self.query_one("#model", Input).value = cur["model"]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id in ("save", ""):
            self._save()

    def _save(self) -> None:
        tier = str(self.query_one("#tier", Select).value or "Basic")
        provider = self.query_one("#provider", Input).value.strip()
        model = self.query_one("#model", Input).value.strip() or None
        if not provider:
            result = self.service.clear_tier(tier)
        else:
            err = validate_name(provider, self.language)
            if err:
                return self.notify(
                    translate(self.language, "ui.config.invalid_provider", error=err),
                    severity="error",
                )
            result = self.service.set_tier(tier, provider, model)
        self.notify(result.message, severity="error" if not result.ok else "information")
        if result.ok:
            self.dismiss(None)
