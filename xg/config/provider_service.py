"""Provider 配置服务：inline 斜杠命令与 TUI 面板共享的 CRUD 入口。

设计口径：
- config.json 是 provider 定义与 API Key 的唯一存储，不再写 .env。
- 写 config.json 复用 :class:`xg.config.manager.ConfigManager` 的分层读写。
- 所有写入在保存前经过校验（api_base 合法、default_model 必填、name 合法、
  key 非占位值），并把错误前移到输入阶段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from xg.config.manager import ConfigManager, _is_placeholder, mask_key
from xg.tui.i18n import UiLanguage, normalize_language, translate

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_ALLOWED_FIELDS = ("api_base", "default_model", "display_name")


@dataclass
class OpResult:
    ok: bool
    message: str
    data: object | None = None


def validate_api_base(url: str, language: UiLanguage = "zh") -> str | None:
    """校验 api_base；非法时返回错误提示，合法返回 None（对齐反引号坑 J6）。"""
    if not url or url != url.strip():
        return translate(language, "ui.validation.api_base_empty")
    if any(ch in url for ch in ("`", " ")):
        return translate(language, "ui.validation.api_base_chars")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        return translate(language, "ui.validation.api_base_parse", error=exc)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return translate(language, "ui.validation.api_base_url")
    return None


def validate_name(name: str, language: UiLanguage = "zh") -> str | None:
    """校验 provider 名称；非法时返回提示。"""
    if not name:
        return translate(language, "ui.validation.provider_name_empty")
    if not _NAME_RE.fullmatch(name):
        return translate(language, "ui.validation.provider_name_chars")
    return None


def validate_model(model: str, language: UiLanguage = "zh") -> str | None:
    """校验模型名；非法时返回提示（非法字符/空白）。"""
    if not model or model != model.strip():
        return translate(language, "ui.validation.model_empty")
    if any(ch in model for ch in ("`", " ")):
        return translate(language, "ui.validation.model_chars")
    return None


class ProviderConfigService:
    """面向界面的 provider 管理服务（无内置预设，纯用户自定义）。"""

    def __init__(self, manager: ConfigManager, settings: object | None = None, language: UiLanguage | None = None) -> None:
        self.manager = manager
        self.settings = settings
        self.language = normalize_language(language if language is not None else getattr(settings, "ui_language", "zh"))

    def _t(self, key: str, **values: object) -> str:
        return translate(self.language, key, **values)

    # ---------- 读取 ----------

    def _active_provider(self) -> str:
        if self.settings is not None and getattr(self.settings, "provider", ""):
            return str(self.settings.provider)
        try:
            return self.manager.active().provider_name
        except Exception:
            return ""

    def list(self) -> list[dict]:
        """返回每个 provider 的视图：脱敏、来源层、是否 base、key 是否已配置。"""
        active = self._active_provider()
        rows = []
        for name in self.manager.provider_names():
            provider = self.manager.resolve_provider(name)
            if provider is None:
                continue
            raw = provider.api_key
            configured = bool(raw) and not _is_placeholder(raw)
            rows.append(
                {
                    "name": name,
                    "display_name": provider.display_name,
                    "api_base": provider.api_base,
                    "default_model": provider.default_model,
                    "models": list(provider.models),
                    "has_key": configured,
                    "is_base": name == active,
                    "layer": self.manager.provider_layer(name) or "user",
                }
            )
        return rows

    def get(self, name: str) -> dict | None:
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return None
        raw = provider.api_key
        configured = bool(raw) and not _is_placeholder(raw)
        return {
            "name": name,
            "display_name": provider.display_name,
            "api_base": provider.api_base,
            "default_model": provider.default_model,
            "models": list(provider.models),
            "api_key": raw if configured else "",
            "api_key_masked": mask_key(raw if configured else ""),
            "has_key": configured,
            "is_base": name == self._active_provider(),
            "layer": self.manager.provider_layer(name) or "user",
        }

    def referenced_by(self, name: str) -> list[str]:
        """返回引用该 provider 的 SmartRouter 档位名（缺省空）。"""
        tiers = (self.manager.smart_router_config().get("tiers") or {})
        return [t for t, cfg in tiers.items() if cfg.get("provider") == name]

    # ---------- 写入 ----------

    def add(
        self,
        name: str,
        api_base: str,
        default_model: str,
        display_name: str | None = None,
        api_key: str | None = None,
    ) -> OpResult:
        if self.manager.resolve_provider(name) is not None:
            return OpResult(False, self._t("ui.provider.exists", name=name) + "（J4）" if self.language == "zh" else self._t("ui.provider.exists", name=name) + " (J4)")
        err = self._validate_fields(api_base, default_model)
        if err:
            return OpResult(False, err)
        fields = {
            "api_base": api_base.strip(),
            "default_model": default_model.strip(),
            "display_name": display_name.strip() if display_name else None,
        }
        layer = self.manager.upsert_provider(name, fields)
        out = self._t("ui.provider.added", name=name, layer=layer)
        if api_key:
            key_result = self.set_api_key(name, api_key)
            if not key_result.ok:
                out += f"{key_result.message}"
        return OpResult(True, out, {"layer": layer})

    def update(self, name: str, fields: dict) -> OpResult:
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown_available", name=name, available=', '.join(self.manager.provider_names()) or self._t("ui.config.value_unset")))
        unknown = [k for k in fields if k not in _ALLOWED_FIELDS]
        if unknown:
            return OpResult(False, (f"Unsupported fields: {', '.join(unknown)}; available: {', '.join(_ALLOWED_FIELDS)}" if self.language == "en" else f"不支持字段: {', '.join(unknown)}；可用: {', '.join(_ALLOWED_FIELDS)}"))
        api_base = fields.get("api_base")
        if api_base is not None:
            err = validate_api_base(str(api_base), self.language)
            if err:
                return OpResult(False, err)
        default_model = fields.get("default_model")
        if default_model is not None and not str(default_model).strip():
            return OpResult(False, "default_model is required and cannot be empty (J7)" if self.language == "en" else "default_model 必填，不能为空（J7）")
        normalized = {}
        if api_base is not None:
            normalized["api_base"] = str(api_base).strip()
        if default_model is not None:
            normalized["default_model"] = str(default_model).strip()
        if "display_name" in fields:
            normalized["display_name"] = (
                str(fields["display_name"]).strip() if fields["display_name"] or fields["display_name"] == "" else None
            )
        layer = self.manager.upsert_provider(name, normalized)
        return OpResult(True, self._t("ui.provider.updated", name=name, layer=layer))

    def remove(self, name: str, *, yes: bool = False) -> OpResult:
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown", name=name))
        if name == self._active_provider():
            return OpResult(
                False,
                self._t("ui.provider.cannot_delete_base", name=name),
            )
        referenced = self.referenced_by(name)
        warnings = []
        if referenced:
            warnings.append(self._t("ui.provider.referenced", tiers=', '.join(referenced)))
        if not yes:
            head = "; ".join(warnings) + "." if warnings and self.language == "en" else "；".join(warnings) + "。" if warnings else self._t("ui.provider.delete_warning")
            return OpResult(False, f"{head} {self._t('ui.provider.delete_confirmation', name=name)}")
        self.manager.delete_provider(name)
        msg = self._t("ui.provider.deleted", name=name)
        if warnings:
            msg += " (" + "; ".join(warnings) + ")" if self.language == "en" else "（" + "；".join(warnings) + "）"
        return OpResult(True, msg)

    def switch(self, name: str, model: str | None = None) -> OpResult:
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown_available", name=name, available=', '.join(self.manager.provider_names()) or self._t("ui.config.value_unset")))
        target_model = (model or "").strip() or provider.default_model
        self.manager.set_active(provider.name, target_model)
        return OpResult(True, self._t("ui.provider.base_switched", provider=provider.name, model=target_model))

    def set_api_key(self, name: str, key: str, *, overwrite: bool = False, yes: bool = False) -> OpResult:
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown", name=name))
        if _is_placeholder(key):
            return OpResult(False, self._t("ui.provider.placeholder_key"))
        existing = provider.api_key
        if existing and not _is_placeholder(existing) and not overwrite:
            if not yes:
                return OpResult(
                    False,
                    self._t("ui.provider.key_overwrite", name=name, masked=mask_key(existing)),
                )
            overwrite = True
        self.manager.upsert_provider(name, {"api_key": key})
        status = ("written" if self.language == "en" else "已写入") if (existing and existing != key) or not existing else ("unchanged" if self.language == "en" else "未改动")
        extra = (f" (replaced old value {mask_key(existing)})" if self.language == "en" else f"（覆盖旧值 {mask_key(existing)}）") if existing and existing != key else ""
        return OpResult(True, self._t("ui.provider.key_written", status=status, name=name, masked=mask_key(key), extra=extra))

    def add_model(self, name: str, model: str) -> OpResult:
        """把模型加入 provider 的模型列表（去重；写入生效层）。"""
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown_available", name=name, available=', '.join(self.manager.provider_names()) or self._t("ui.config.value_unset")))
        err = validate_model(model, self.language)
        if err:
            return OpResult(False, err)
        model = model.strip()
        current = list(provider.models)
        if model in current:
            return OpResult(False, self._t("ui.provider.model_exists", name=name, model=model))
        current.append(model)
        layer = self.manager.upsert_provider(name, {"models": current})
        if self.language == "zh":
            return OpResult(True, f"已为 {name} 添加模型: {model}（写入 {layer} 配置。可用: /model {model} 切换）")
        return OpResult(True, self._t("ui.provider.model_added", name=name, model=model, layer=layer))

    def remove_model(self, name: str, model: str) -> OpResult:
        """把模型从 provider 的模型列表移除。"""
        provider = self.manager.resolve_provider(name)
        if provider is None:
            return OpResult(False, self._t("ui.provider.unknown_available", name=name, available=', '.join(self.manager.provider_names()) or self._t("ui.config.value_unset")))
        model = model.strip()
        if model not in provider.models:
            return OpResult(False, self._t("ui.provider.model_missing", name=name, model=model))
        remaining = [m for m in provider.models if m != model]
        layer = self.manager.upsert_provider(name, {"models": remaining})
        return OpResult(True, self._t("ui.provider.model_removed", name=name, model=model, layer=layer))

    # ---------- 校验 ----------

    def _validate_fields(self, api_base: str, default_model: str) -> str | None:
        err = validate_api_base(str(api_base), self.language)
        if err:
            return err
        if not str(default_model).strip():
            return "default_model is required and cannot be empty (J7)" if self.language == "en" else "default_model 必填，不能为空（J7）"
        return None
