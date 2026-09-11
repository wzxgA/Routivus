"""Web Console Composer 的 slash 命令补全（只读、无副作用）。

把 `routivus/cli/completion.py` 的纯补全引擎接到 REST 端点上：静态候选
（命令 → 子命令 → 选项）直接复用引擎；动态候选按两种来源展开——

- 工作区路径（`/team resume … --write-scope <路径>`）：按会话所属项目的 root_path
- 动态值（`/model dee` 的模型名、`/memory delete` 的记忆 ID）：按会话 agent 的
  ConfigManager / memory_manager 实时生成（与 TUI 的
  `tui/controller.py:completion_registry` 同构）

白名单与命令执行通道保持同步：`server/app.py` 的命令分派真正执行哪些命令，
这里就提示哪些（见 plans/enhancement/01-web-slash-commands.md §3 的矩阵）。
提示一个补全了却执行不了的命令是假动作。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Web Console 会话内真正会分派的顶层命令（保持与 app.py 的命令分派同步）。
WEB_COMMANDS: tuple[str, ...] = (
    "/plan",
    "/team",
    "/help",
    "/model",
    "/smartrouter",
    "/tier",
    "/provider",
    "/config",
    "/hitl",
    "/memory",
    "/save",
    "/lang",
    "/clear",
    "/cancel",
)


def _empty(raw: str) -> dict[str, Any]:
    """非命令行 / 无法解析时的统一空载荷（replace 区间指向行尾）。"""
    return {
        "is_command": False,
        "replace_start": len(raw),
        "replace_end": len(raw),
        "candidates": [],
    }


def _dynamic_registry(manager: Any = None, agent: Any = None) -> Any:
    """构建动态值候选的 provider 注册表（与 TUI 的 completion_registry 同构）。

    每个 provider 只读本地内存/配置，绝不触发 LLM、网络或工具执行。任何读取
    失败都只让该 provider 变成空候选，不影响补全本身。
    """
    from routivus.cli.completion import CompletionCandidate, CompletionProviderRegistry

    reg = CompletionProviderRegistry()

    def _providers() -> list[str]:
        return list(manager.provider_names()) if manager is not None else []

    def _models() -> list[str]:
        if manager is None:
            return []
        seen: set[str] = set()
        try:
            seen.add(manager.active().model)
        except Exception:  # noqa: BLE001 - 尚未配置 provider 是正常初始态
            pass
        for name in manager.provider_names():
            provider = manager.resolve_provider(name)
            if provider is not None:
                # /model 允许切到 default_model + models 全集，候选保持一致。
                seen.add(provider.default_model)
                seen.update(provider.models)
        return sorted(seen)

    reg.register(
        "provider",
        lambda ctx: [
            CompletionCandidate(name, name, detail="provider", kind="value")
            for name in _providers()
        ],
    )
    reg.register(
        "model",
        lambda ctx: [
            CompletionCandidate(model, model, detail="model", kind="value")
            for model in _models()
        ],
    )

    memory = getattr(agent, "memory_manager", None)
    reg.register(
        "memory_id",
        lambda ctx: [
            CompletionCandidate(str(entry.id), str(entry.id), detail="记忆", kind="value")
            for entry in (memory.list(20) if memory is not None else [])
        ],
    )
    return reg


def completion_payload(
    raw: str,
    cursor: int | None = None,
    *,
    project_root: Path | None = None,
    manager: Any = None,
    agent: Any = None,
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

    allowed = {name.lower() for name in WEB_COMMANDS}
    known: set[str] = set()
    for spec in SLASH_COMMANDS:
        known.add(spec.name.lower())
        for alias in spec.aliases:
            known.add(alias.lower())

    command = ctx.command.lower()
    if command in allowed:
        # 白名单命令内部：静态层（子命令 / 选项）+ 动态值层 + 动态路径层。
        candidates = list(engine.completion_candidates(raw, cursor))
        try:
            candidates.extend(
                engine.dynamic_candidates(raw, cursor, _dynamic_registry(manager, agent))
            )
        except Exception:  # noqa: BLE001 - 动态层失败不拖垮静态候选
            pass
        if project_root is not None:
            candidates.extend(
                engine.path_completion_candidates(raw, cursor, project_root)
            )
    elif command not in known:
        # 还在敲顶层命令 token（例如 "/mo"）：只在白名单里做前缀匹配。
        candidates = [
            cand
            for cand in engine.completion_candidates(raw, cursor)
            if cand.insert_text.lower() in allowed
        ]
    else:
        # 已解析到非白名单命令：Web 端执行不了，不给候选。
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
