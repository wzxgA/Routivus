"""SmartRouter 配置服务（config.json 介质，供 /tier 命令与 TUI 面板使用）。

仅读写 config.json 的 ``smart_router`` 节点；不再依赖 XG_SMART_ROUTER* 环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xg.config.manager import ConfigManager, _SMART_ROUTER_TIERS
from xg.config.provider_service import validate_model
from xg.tui.i18n import UiLanguage, normalize_language, translate


@dataclass(frozen=True)
class OpResult:
    ok: bool
    message: str


class SmartRouterConfigService:
    def __init__(self, manager: ConfigManager, settings: Any = None, language: UiLanguage | None = None) -> None:
        self.manager = manager
        # 运行的 Settings 对象（可选）；用于配置档位时同步运行态开关，
        # 使“自动开启”在当前会话立即生效，而非仅持久化到 config.json。
        self._settings = settings
        self.language = normalize_language(language if language is not None else getattr(settings, "ui_language", "zh"))

    def _t(self, key: str, **values: object) -> str:
        return translate(self.language, key, **values)

    # ---------- 读取 ----------

    def get(self) -> dict:
        """返回 {enabled, tiers}；tiers 为 {档位: {provider, model}}。"""
        return self.manager.smart_router_config()

    def skip_config_relies_on_active(self, raw: dict) -> str:
        """未显式配置某档时的回落标记用（见 list_tiers）。"""
        configured = raw.get("provider") and raw.get("model")
        return self._t("ui.tier.configured") if configured else self._t("ui.tier.fallback")

    def list_tiers(self) -> list[dict]:
        """按固定四档顺序返回每档 {name, provider, model, configured}。"""
        cfg = self.get()
        tiers = cfg.get("tiers") or {}
        rows: list[dict] = []
        for name in _SMART_ROUTER_TIERS:
            entry = tiers.get(name) or {}
            provider = str(entry.get("provider", "") or "")
            model = str(entry.get("model", "") or "")
            rows.append({
                "name": name,
                "provider": provider,
                "model": model,
                "configured": bool(provider and model),
            })
        return rows

    def get_tier(self, tier: str) -> tuple[OpResult, dict | None]:
        """查看单个档位。"""
        err = self._validate_tier(tier)
        if err:
            return OpResult(False, err), None
        cfg = self.get()
        entry = (cfg.get("tiers") or {}).get(tier) or {}
        return OpResult(True, ""), {
            "name": tier,
            "provider": str(entry.get("provider", "") or ""),
            "model": str(entry.get("model", "") or ""),
        }

    # ---------- 写入 ----------

    def set_tier(self, tier: str, provider: str, model: str | None = None) -> OpResult:
        """设置档位 provider/model。宽松校验：provider 必须存在；model 任意非空合法串。"""
        err = self._validate_tier(tier)
        if err:
            return OpResult(False, err)

        p = provider.strip()
        prov = self.manager.resolve_provider(p) if p else None
        if p is None or prov is None:
            return OpResult(False, self._t("ui.provider.unknown", name=p))

        resolved_model: str
        if model is None or model.strip() == "":
            resolved_model = prov.default_model or "default"
        else:
            resolved_model = model.strip()
        merr = validate_model(resolved_model, self.language)
        if merr:
            return OpResult(False, merr)

        was_enabled = bool(self.get().get("enabled", False))
        self.manager.set_smart_router_tier(tier, prov.name, resolved_model)
        msg = self._t("ui.tier.set", tier=tier, provider=prov.name, model=resolved_model)
        if not was_enabled:
            # 配置/修改档位后自动生效：开启开关并同步运行态。
            self.manager.set_smart_router_enabled(True)
            if self._settings is not None:
                self._settings.smart_router_enabled = True
            msg += self._t("ui.tier.auto_enabled")
        return OpResult(True, msg)

    def clear_tier(self, tier: str) -> OpResult:
        err = self._validate_tier(tier)
        if err:
            return OpResult(False, err)
        removed = self.manager.remove_smart_router_tier(tier)
        if not removed:
            return OpResult(True, self._t("ui.tier.not_configured", tier=tier))
        return OpResult(True, self._t("ui.tier.cleared", tier=tier))

    def set_enabled(self, enabled: bool) -> OpResult:
        self.manager.set_smart_router_enabled(bool(enabled))
        return OpResult(True, self._t("ui.tier.enabled" if enabled else "ui.tier.disabled"))

    def _validate_tier(self, tier: str) -> str | None:
        if tier not in _SMART_ROUTER_TIERS:
            return self._t("ui.tier.unknown", tier=tier, tiers="/".join(_SMART_ROUTER_TIERS))
        return None
