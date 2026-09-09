"""MCP server configuration loading, merging and environment expansion."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from routivus.mcp.models import McpServerConfig


MCP_CONFIG_FILE = "mcp.json"
_SERVER_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LEVELS = {"default", "never", "confirm", "always"}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class McpConfigLoadResult:
    servers: dict[str, McpServerConfig]
    errors: tuple[str, ...] = ()


class McpConfigManager:
    def __init__(
        self,
        *,
        user_dir: str | Path | None = None,
        project_root: str | Path | None = None,
        env: dict[str, str] | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self.user_dir = Path(user_dir) if user_dir else Path.home() / ".xg"
        self.project_root = (Path(project_root) if project_root else Path.cwd()).resolve()
        self.user_config_path = self.user_dir / MCP_CONFIG_FILE
        self.project_config_path = self.project_root / ".xg" / MCP_CONFIG_FILE
        self.env = env if env is not None else os.environ
        self.defaults = {
            "startup_timeout": 15.0,
            "request_timeout": 120.0,
            "shutdown_timeout": 5.0,
            "max_output_chars": 20_000,
            "max_tools": 256,
            "max_resources": 512,
            "max_message_bytes": 2_097_152,
            "resource_max_chars": 32_000,
            "log_lines": 200,
            **(defaults or {}),
        }

    def _read(self, path: Path) -> tuple[dict, str | None]:
        if not path.is_file():
            return {}, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {}, f"{path}: JSON 格式错误（{exc.lineno}:{exc.colno}）"
        except OSError as exc:
            return {}, f"{path}: 读取失败（{exc}）"
        if not isinstance(data, dict):
            return {}, f"{path}: 顶层必须是 JSON 对象"
        return data, None

    def _expand(self, value: Any, missing: set[str]) -> Any:
        if isinstance(value, str):
            def replace(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in self.env:
                    missing.add(name)
                    return ""
                return str(self.env[name])
            return _ENV_RE.sub(replace, value)
        if isinstance(value, list):
            return [self._expand(item, missing) for item in value]
        if isinstance(value, dict):
            return {str(key): self._expand(item, missing) for key, item in value.items()}
        return value

    @staticmethod
    def _number(raw: dict, name: str, default: float, minimum: float = 0.1) -> float:
        try:
            return max(minimum, float(raw.get(name, default)))
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _integer(raw: dict, name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(raw.get(name, default)))
        except (TypeError, ValueError):
            return int(default)

    def load(self) -> McpConfigLoadResult:
        user, user_error = self._read(self.user_config_path)
        project, project_error = self._read(self.project_config_path)
        errors = [error for error in (user_error, project_error) if error]
        merged = _deep_merge(user, project)
        raw_servers = merged.get("servers", {})
        if not isinstance(raw_servers, dict):
            return McpConfigLoadResult({}, tuple([*errors, "mcp.json 的 servers 必须是对象"]))

        servers: dict[str, McpServerConfig] = {}
        enabled_overrides = user.get("enabled_overrides", {})
        if not isinstance(enabled_overrides, dict):
            enabled_overrides = {}
        for name, original in raw_servers.items():
            if not _SERVER_RE.fullmatch(str(name)):
                errors.append(f"MCP Server 名称无效: {name}")
                continue
            if not isinstance(original, dict):
                errors.append(f"MCP Server {name} 配置必须是对象")
                continue
            missing: set[str] = set()
            source = dict(original)
            if name in enabled_overrides:
                source["enabled"] = bool(enabled_overrides[name])
            raw = self._expand(source, missing)
            if missing:
                errors.append(f"MCP Server {name} 缺少环境变量: {', '.join(sorted(missing))}")
                continue
            try:
                config = self._parse_server(str(name), raw)
            except ValueError as exc:
                errors.append(f"MCP Server {name}: {exc}")
                continue
            servers[name] = config
        return McpConfigLoadResult(servers, tuple(errors))

    def _parse_server(self, name: str, raw: dict) -> McpServerConfig:
        transport = str(raw.get("transport", "stdio")).lower().replace("-", "_")
        if transport == "http":
            transport = "streamable_http"
        if transport not in {"stdio", "streamable_http"}:
            raise ValueError("transport 必须是 stdio 或 streamable_http")
        command = str(raw.get("command", "")).strip() or None
        url = str(raw.get("url", "")).strip() or None
        if transport == "stdio" and not command:
            raise ValueError("stdio transport 缺少 command")
        if transport == "streamable_http":
            if not url:
                raise ValueError("streamable_http transport 缺少 url")
            parsed_url = urlparse(url)
            if parsed_url.scheme.lower() not in {"http", "https"}:
                raise ValueError("url 只允许 http/https")
            if parsed_url.username or parsed_url.password:
                raise ValueError("url 禁止内嵌用户名或密码，请使用 headers + ${VAR}")

        args = raw.get("args", [])
        env = raw.get("env", {})
        headers = raw.get("headers", {})
        overrides = raw.get("tool_overrides", {})
        if not isinstance(args, list) or not all(isinstance(item, (str, int, float)) for item in args):
            raise ValueError("args 必须是字符串数组")
        if not isinstance(env, dict) or not isinstance(headers, dict):
            raise ValueError("env/headers 必须是对象")
        if not isinstance(overrides, dict):
            raise ValueError("tool_overrides 必须是对象")
        hitl = str(raw.get("hitl", "default")).lower()
        if hitl not in _LEVELS:
            raise ValueError("hitl 必须是 default/never/confirm/always")
        clean_overrides: dict[str, str] = {}
        for tool_name, level in overrides.items():
            level = str(level).lower()
            if level not in _LEVELS - {"default"}:
                raise ValueError(f"工具 {tool_name} 的审批级别无效")
            clean_overrides[str(tool_name)] = level

        cwd = str(raw.get("cwd", "")).strip() or None
        if cwd:
            cwd_path = Path(cwd)
            if not cwd_path.is_absolute():
                cwd_path = self.project_root / cwd_path
            cwd_path = cwd_path.resolve()
            if not cwd_path.is_relative_to(self.project_root):
                raise ValueError("cwd 必须位于项目根目录内")
            cwd = str(cwd_path)
        elif transport == "stdio":
            cwd = str(self.project_root)

        return McpServerConfig(
            name=name,
            transport=transport,  # type: ignore[arg-type]
            enabled=bool(raw.get("enabled", True)),
            command=command,
            args=tuple(str(item) for item in args),
            cwd=cwd,
            env={str(key): str(value) for key, value in env.items()},
            url=url,
            headers={str(key): str(value) for key, value in headers.items()},
            startup_timeout=self._number(raw, "startup_timeout", self.defaults["startup_timeout"]),
            request_timeout=self._number(raw, "request_timeout", self.defaults["request_timeout"]),
            shutdown_timeout=self._number(raw, "shutdown_timeout", self.defaults["shutdown_timeout"]),
            max_output_chars=self._integer(raw, "max_output_chars", self.defaults["max_output_chars"]),
            hitl=hitl,  # type: ignore[arg-type]
            tool_overrides=clean_overrides,
            max_tools=self._integer(raw, "max_tools", self.defaults["max_tools"]),
            max_resources=self._integer(raw, "max_resources", self.defaults["max_resources"]),
            max_message_bytes=self._integer(raw, "max_message_bytes", self.defaults["max_message_bytes"], 1024),
            resource_max_chars=self._integer(raw, "resource_max_chars", self.defaults["resource_max_chars"]),
            log_lines=self._integer(raw, "log_lines", self.defaults["log_lines"]),
        )

    def set_enabled(self, name: str, enabled: bool) -> None:
        data, error = self._read(self.user_config_path)
        if error:
            raise ValueError(error)
        overrides = data.setdefault("enabled_overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
            data["enabled_overrides"] = overrides
        overrides[name] = enabled
        self.user_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
