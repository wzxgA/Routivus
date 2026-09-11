"""Web Console Composer 的 slash 命令补全（只读、无副作用）。

把 `routivus/cli/completion.py` 的纯补全引擎接到 REST 端点上：静态候选
（命令 → 子命令 → 选项）直接复用引擎；动态路径候选按会话所属项目的
root_path 展开（`/team resume … --write-scope <路径>`）。

为什么只提示一部分命令：桌面端聊天 WS（`server/app.py` 的
`_parse_task_command`）真正执行的顶层命令只有 `/plan` 与 `/team`，其余 TUI
命令（/model、/smartrouter、/tier …）在 Web Console 走配置页 / 顶栏（见
README「SmartRouter 智能路由」一节）。提示一个补全了却执行不了的命令是
假动作，所以这里按白名单过滤；等 Web 端接入 service 命令分发后扩大白名单即可。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 桌面端聊天 WS 实际会分派的顶层命令（保持与 _parse_task_command 同步）。
DESKTOP_COMMANDS: tuple[str, ...] = ("/plan", "/team")


def _empty(raw: str) -> dict[str, Any]:
    """非命令行 / 无法解析时的统一空载荷（replace 区间指向行尾）。"""
    return {
        "is_command": False,
        "replace_start": len(raw),
        "replace_end": len(raw),
        "candidates": [],
    }


def completion_payload(
    raw: str,
    cursor: int | None = None,
    *,
    project_root: Path | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """计算一行 Composer 输入的补全候选。

    任何输入都不抛异常：畸形 / 空输入只会得到空候选。返回结构：

        {"is_command": bool,
         "replace_start": int,   # 应用候选时 client 端替换的区间（当前 token）
         "replace_end": int,
         "candidates": [{"label", "insert_text", "detail", "kind"}]}
    """
    from routivus.cli import completion as engine
    from routivus.cli.commands import SLASH_COMMANDS

    if not isinstance(raw, str) or not raw.lstrip().startswith("/"):
        return _empty(raw if isinstance(raw, str) else "")

    ctx = engine.parse_completion_line(raw, cursor)
    if not ctx.is_command:
        return _empty(raw)

    allowed = {name.lower() for name in DESKTOP_COMMANDS}
    known: set[str] = set()
    for spec in SLASH_COMMANDS:
        known.add(spec.name.lower())
        for alias in spec.aliases:
            known.add(alias.lower())

    command = ctx.command.lower()
    if command in allowed:
        # 白名单命令内部：静态层（子命令 / 选项）+ 动态路径层。
        candidates = list(engine.completion_candidates(raw, cursor))
        if project_root is not None:
            candidates.extend(
                engine.path_completion_candidates(raw, cursor, project_root)
            )
    elif command not in known:
        # 还在敲顶层命令 token（例如 "/pl"）：只在白名单里做前缀匹配。
        candidates = [
            cand
            for cand in engine.completion_candidates(raw, cursor)
            if cand.insert_text.lower() in allowed
        ]
    else:
        # 已解析到非白名单命令（例如 "/model dee"）：桌面端执行不了，不给候选。
        candidates = []

    # 去重并保持稳定顺序（静态在前、路径在后），总量截断防刷屏。
    seen: set[str] = set()
    merged: list[Any] = []
    for cand in candidates:
        if cand.insert_text in seen:
            continue
        seen.add(cand.insert_text)
        merged.append(cand)
        if len(merged) >= limit:
            break

    return {
        "is_command": True,
        "replace_start": ctx.current_token_start,
        "replace_end": ctx.current_token_end,
        "candidates": [
            {
                "label": cand.label,
                "insert_text": cand.insert_text,
                "detail": cand.detail,
                "kind": cand.kind,
            }
            for cand in merged
        ],
    }
