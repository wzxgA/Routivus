"""Web configuration loading with user/project layering and env expansion."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from routivus.web.models import WebConfig, WebFetchConfig, WebSearchConfig

WEB_CONFIG_FILE = "web.json"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class WebConfigManager:
    def __init__(self, *, user_dir: str | Path | None = None,
                 project_root: str | Path | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.user_dir = Path(user_dir) if user_dir else Path.home() / ".routivus"
        self.project_root = (Path(project_root) if project_root else Path.cwd()).resolve()
        self.user_config_path = self.user_dir / WEB_CONFIG_FILE
        self.project_config_path = self.project_root / ".routivus" / WEB_CONFIG_FILE
        self.env = env if env is not None else os.environ
        self.errors: list[str] = []

    def _read(self, path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f"{path}: 配置读取失败")
            return {}
        if not isinstance(value, dict):
            self.errors.append(f"{path}: 顶层必须是 JSON 对象")
            return {}
        return value

    def _expand(self, value: Any) -> Any:
        if isinstance(value, str):
            return _ENV_RE.sub(lambda m: str(self.env.get(m.group(1), "")), value)
        if isinstance(value, list):
            return [self._expand(item) for item in value]
        if isinstance(value, dict):
            return {str(k): self._expand(v) for k, v in value.items()}
        return value

    @staticmethod
    def _float(value: Any, default: float, minimum: float = 0.1) -> float:
        try:
            return max(minimum, float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int(value: Any, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return default

    def load(self) -> WebConfig:
        self.errors.clear()
        raw = self._expand(_merge(self._read(self.user_config_path), self._read(self.project_config_path)))
        search_raw = raw.get("search") if isinstance(raw.get("search"), dict) else {}
        fetch_raw = raw.get("fetch") if isinstance(raw.get("fetch"), dict) else {}
        env = self.env
        provider = str(env.get("ROUTIVUS_WEB_SEARCH_PROVIDER", search_raw.get("provider", "none")) or "none").lower()
        providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
        providers = {str(k): dict(v) for k, v in providers.items() if isinstance(v, dict)}
        selected = providers.get(provider, {})
        api_key_env = str(selected.get("api_key_env") or {
            "zhipu": "ROUTIVUS_ZHIPU_SEARCH_API_KEY",
            "serpapi": "ROUTIVUS_SERPAPI_API_KEY",
        }.get(provider, ""))
        api_key = selected.get("api_key") or env.get(api_key_env, "")
        api_base = selected.get("api_base") or env.get({
            "zhipu": "ROUTIVUS_ZHIPU_SEARCH_API_BASE",
            "serpapi": "ROUTIVUS_SERPAPI_API_BASE",
        }.get(provider, ""), "") or None
        if not api_base and provider == "zhipu":
            api_base = "https://open.bigmodel.cn/api/paas/v4"
        if not api_base and provider == "serpapi":
            api_base = "https://serpapi.com"
        if provider == "searxng":
            api_base = selected.get("url") or env.get("ROUTIVUS_SEARXNG_URL", "") or None
        search = WebSearchConfig(
            provider=provider,
            api_base=str(api_base) if api_base else None,
            api_key_env=api_key_env or None,
            api_key=str(api_key) if api_key else None,
            timeout=self._float(env.get("ROUTIVUS_WEB_TIMEOUT", search_raw.get("timeout", 15)), 15.0),
            max_results=min(10, self._int(env.get("ROUTIVUS_WEB_MAX_RESULTS", search_raw.get("max_results", 5)), 5)),
            rate_limit_per_minute=self._int(env.get("ROUTIVUS_WEB_RATE_LIMIT_PER_MINUTE", search_raw.get("rate_limit_per_minute", 30)), 30),
            enabled=provider != "none" and bool(raw.get("enabled", True)),
        )
        allowed = fetch_raw.get("allowed_ports", (80, 443))
        if not isinstance(allowed, (list, tuple)):
            allowed = (80, 443)
        ports = tuple(self._int(p, 80) for p in allowed)
        fetch = WebFetchConfig(
            timeout=self._float(env.get("ROUTIVUS_WEB_TIMEOUT", fetch_raw.get("timeout", 15)), 15.0),
            max_response_bytes=self._int(env.get("ROUTIVUS_WEB_MAX_RESPONSE_BYTES", fetch_raw.get("max_response_bytes", 2 * 1024 * 1024)), 2 * 1024 * 1024, 1024),
            max_chars=self._int(env.get("ROUTIVUS_WEB_FETCH_MAX_CHARS", fetch_raw.get("max_chars", 32_000)), 32_000, 256),
            max_redirects=self._int(env.get("ROUTIVUS_WEB_MAX_REDIRECTS", fetch_raw.get("max_redirects", 5)), 5, 0),
            allowed_ports=ports,
        )
        enabled = env.get("ROUTIVUS_WEB_ENABLED", "on").lower() not in ("off", "0", "false") and bool(raw.get("enabled", True))
        rate_limit = self._int(env.get("ROUTIVUS_WEB_RATE_LIMIT_PER_MINUTE", search_raw.get("rate_limit_per_minute", 30)), 30)
        return WebConfig(enabled=enabled, search=search, fetch=fetch, providers=providers,
                         rate_limit_per_minute=rate_limit)

    def snapshot(self) -> dict[str, Any]:
        config = self.load()
        return {
            "enabled": config.enabled,
            "search_provider": config.search.provider,
            "search_configured": bool(config.search.api_base and (config.search.api_key or config.search.provider == "searxng")),
            "fetch": {"timeout": config.fetch.timeout, "max_response_bytes": config.fetch.max_response_bytes,
                      "max_chars": config.fetch.max_chars, "max_redirects": config.fetch.max_redirects},
        }


def load_web_config(*, user_dir=None, project_root=None, env=None) -> WebConfig:
    return WebConfigManager(user_dir=user_dir, project_root=project_root, env=env).load()


load_web_settings = load_web_config
