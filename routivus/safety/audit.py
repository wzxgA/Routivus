"""审计日志：统一 JSONL 记录 + 敏感字段脱敏。

记录位置：<项目根>/.routivus/audit.log
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

_SENSITIVE_KEY_RE = re.compile(r"^(api_key|apikey|token|authorization|password|secret|private_key)$", re.I)
_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
# 注意 `token` 是裸词分支：终端通道会把整条命令行写入审计，`--token=xxx` 这类
# 常见写法必须被覆盖（`access_token` 分支匹配不到它）。
_SECRET_ASSIGN_RE = re.compile(
    r"\b(api[_-]?key|access[_-]?token|token|auth(?:orization)?|password|secret)"
    r"(\s*[:=]\s*)(?!Bearer\b)([^\s,;]+)",
    re.I,
)
_REDACTED = "***"


def _web_audit_value(key: str, value: Any) -> Any:
    """Keep Web audit useful without retaining query tokens or full prompts."""
    if key == "query" and isinstance(value, str):
        return redact_text(value[:120])
    if key in {"requested_url", "final_url", "url"} and isinstance(value, str):
        try:
            parts = urlsplit(value)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            return value[:200]
    if key in {"headers", "header", "authorization", "cookie", "set-cookie"}:
        return _REDACTED
    return value


def redact_text(value: str) -> str:
    """脱敏文本中的 Bearer token 与常见 key=value 形式敏感值。"""
    value = _BEARER_RE.sub(r"\1***", value)
    return _SECRET_ASSIGN_RE.sub(r"\1\2***", value)


def redact(value: Any) -> Any:
    """递归脱敏：敏感键名的值、文本中的 Bearer token（保留键名，脱敏值）。"""
    if isinstance(value, dict):
        return {
            str(k): _REDACTED if _SENSITIVE_KEY_RE.match(str(k)) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class AuditLogger:
    def __init__(self, log_path: str | Path, session_id: str | None = None) -> None:
        self.log_path = Path(log_path)
        self.session_id = session_id or uuid4().hex[:8]

    def record(self, action: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "session": self.session_id,
            "action": action,
        }
        entry.update({k: redact(_web_audit_value(k, v) if action.startswith("web_") else v) for k, v in fields.items()})
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def tool_call(self, tool: str, args: dict, ok: bool, duration_ms: int, approved: bool = True) -> None:
        if tool.startswith("web_"):
            args = {key: _web_audit_value(key, value) for key, value in args.items()}
        self.record(
            "tool_call",
            tool=tool,
            args=args,
            approved=approved,
            ok=ok,
            duration_ms=duration_ms,
        )

    def approval(self, tool: str, args: dict, decision: str, reason: str = "") -> None:
        self.record("approval", tool=tool, args=args, decision=decision, reason=reason)

    def blocked(self, reason: str, **detail: Any) -> None:
        self.record("blocked", reason=reason, **detail)

    def terminal_command(
        self,
        command: str,
        *,
        cwd: str,
        ok: bool,
        duration_ms: int = 0,
        exit_code: int | None = None,
        output_chars: int = 0,
        terminal_id: str = "",
        reason: str = "",
    ) -> None:
        """记录一次终端通道输入。

        `action` 取 `terminal_command` 而非 `tool_call`，与 Agent 的
        `execute_command` 工具明确区分来源。命令串过 redact_text 脱敏；
        命令输出不入审计（体积大且易含密钥），只记长度与退出码。
        """
        self.record(
            "terminal_command",
            origin="terminal",
            command=redact_text(command),
            cwd=cwd,
            ok=ok,
            duration_ms=duration_ms,
            exit_code=exit_code,
            output_chars=output_chars,
            terminal_id=terminal_id,
            reason=reason,
        )
