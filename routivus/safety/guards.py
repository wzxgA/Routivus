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

# PowerShell 的破坏性写法（方案 09 §4.4）：上面那组是 cmd / POSIX 语法，PS 原生
# cmdlet 不在表内 —— 而终端默认 shell 已是 PowerShell。别名 rm / ri / del / erase /
# rd / rmdir 都指向 Remove-Item，`-r` 是 `-Recurse` 的合法缩写。
# 只补原表覆盖的三类最危险操作（递归删除 / 关机重启 / 磁盘格式化），保持「最小集」。
_PS_DELETE = r"\b(?:remove-item|ri|del|erase|rm|rmdir|rd)\b"
_PS_RECURSE = r"\s-(?:recurse|r)\b"
# 顺序无关：`-Recurse` 写在路径前或后都算（`Remove-Item C:\ -Recurse` 与
# `Remove-Item -Recurse C:\` 同等看待），所以用两个前瞻而不是串联。
_PS_RECURSIVE_DELETE = _PS_DELETE + r"(?=[^\r\n]*" + _PS_RECURSE + r")"

_POWERSHELL_BLACKLIST_PATTERNS = [
    # 递归删除 + 盘符绝对路径（对齐 cmd 的 `del C:\` / `rd /s`；含误伤换安全）
    re.compile(_PS_RECURSIVE_DELETE + r"(?=[^\r\n]*\b[a-z]:\\)"),
    # 递归删除 POSIX 根（对齐 `rm -rf /`）
    re.compile(_PS_RECURSIVE_DELETE + r"(?=[^\r\n]*\s/(?:\s|\*|$))"),
    # 关机 / 重启的 PS 原生 cmdlet（对齐 shutdown / reboot）
    re.compile(r"\b(?:stop-computer|restart-computer)\b"),
    # 磁盘 / 分区级破坏（对齐 format / diskpart）
    re.compile(r"\b(?:format-volume|clear-disk|initialize-disk|new-partition)\b"),
]


def command_guard(command: str) -> GuardResult:
    """校验命令是否命中黑名单。空命令放行（后续由工具层报错）。

    两组模式：POSIX / cmd 语法（`_COMMAND_BLACKLIST_PATTERNS`）与 PowerShell 原生
    写法（`_POWERSHELL_BLACKLIST_PATTERNS`，方案 09 §4.4）。黑名单只看命令字符串、
    看不到 shell 的状态，两组都过一遍才能保证「换默认 shell 不降低安全性」。
    """
    if not command or not command.strip():
        return GuardResult(ok=True)
    lower = command.strip().lower()
    for pattern in (*_COMMAND_BLACKLIST_PATTERNS, *_POWERSHELL_BLACKLIST_PATTERNS):
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
    """校验路径工具的参数是否位于项目根内。

    `execute_command` 没有 `path` 参数，但它的 `cwd` 同样必须落在项目根内 ——
    这正是 path_guard 参与命令类工具校验的原因。
    """
    if tool_name not in PATH_TOOLS and tool_name != "execute_command":
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
    """策略层统一入口。

    终端通道复用本函数而非另起一套，保证终端命令与 Agent 的 execute_command
    走完全相同的校验。注意黑名单只看命令字符串、看不到 shell 的当前目录，
    因此这里拦不住持久化 shell 里的 `cd ..` —— 见 README 的安全边界说明。
    """
    if tool_name == "execute_command":
        result = command_guard(str(args.get("command", "")))
        if not result.ok:
            return result
        return path_guard(base, tool_name, args)
    if tool_name in PATH_TOOLS:
        return path_guard(base, tool_name, args)
    return GuardResult(ok=True)
