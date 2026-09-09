"""PathGuard / CommandGuard：策略层纯函数。

策略层拒绝为终审：黑名单命令 / 越界路径不执行，且不可被 HITL 审批绕过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 受路径约束的工具：read / write / list / glob / grep
PATH_TOOLS = {"read_file", "write_file", "list_dir", "glob_files", "grep_code"}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str = ""   # command_blacklist / path_outside_root
    detail: str = ""


# ---------- CommandGuard ----------

_COMMAND_BLACKLIST_PATTERNS = [
    # 对根/系统目录的递归删除
    re.compile(r"\brm\s+(-rf|-fr)\s+/\s*$"),
    re.compile(r"\brm\s+(-rf|-fr)\s+/\*"),
    # 磁盘/分区级破坏
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.*\bof=/dev/"),
    re.compile(r"\bformat\s+[a-z]:\\?"),
    re.compile(r"\bdiskpart\b"),
    re.compile(r"^\s*>\s*/dev/"),
    # Windows 破坏性删除：递归删除 / 驱动盘根级
    re.compile(r"\bdel\b.*\b/s\b"),
    re.compile(r"\bdel\b.*\b[cd]:\\"),
    re.compile(r"\brd\b.*\b/s\b"),
    re.compile(r"\brd\b.*\b[cd]:\\"),
    # 关机 / 重启
    re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"),
    # fork bomb
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
]


def command_guard(command: str) -> GuardResult:
    """校验命令是否命中黑名单。空命令放行（后续由工具层报错）。"""
    if not command or not command.strip():
        return GuardResult(ok=True)
    lower = command.strip().lower()
    for pattern in _COMMAND_BLACKLIST_PATTERNS:
        if pattern.search(lower):
            return GuardResult(ok=False, reason="command_blacklist", detail=command.strip())
    return GuardResult(ok=True)


# ---------- PathGuard ----------

def _resolve_absolute(base: Path, raw: str) -> Path:
    """把工具路径参数解析为绝对路径（解 symlink）。"""
    p = Path(raw)
    abs_path = p if p.is_absolute() else (base / p)
    return abs_path.resolve()


def path_guard(base: Path, tool_name: str, args: dict) -> GuardResult:
    """校验路径工具的参数是否位于项目根内。"""
    if tool_name not in PATH_TOOLS:
        return GuardResult(ok=True)

    targets: list[tuple[str, str]] = []
    raw_path = args.get("path")
    if raw_path:
        targets.append(("path", str(raw_path)))
    if tool_name in ("write_file", "execute_command"):
        raw_cwd = args.get("cwd")
        if raw_cwd:
            targets.append(("cwd", str(raw_cwd)))

    for label, raw in targets:
        resolved = _resolve_absolute(base, raw)
        if not _is_within(base, resolved):
            return GuardResult(
                ok=False,
                reason="path_outside_root",
                detail=f"{label}={raw}（解析为 {resolved}，不在项目根内）",
            )
    return GuardResult(ok=True)


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base.resolve())
        return True
    except ValueError:
        return False


def guard_tool_call(base: Path, tool_name: str, args: dict) -> GuardResult:
    """策略层统一入口。"""
    if tool_name == "execute_command":
        return command_guard(str(args.get("command", "")))
    if tool_name in PATH_TOOLS:
        return path_guard(base, tool_name, args)
    return GuardResult(ok=True)
