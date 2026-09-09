"""Commands shared by the inline CLI and the fullscreen UI.

The command service deliberately returns data instead of printing it.  The
legacy helpers in :mod:`routivus.cli.app` remain the compatibility implementation
for now; keeping this adapter small lets the TUI use the exact same command
semantics while the inline renderer is migrated incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from routivus.tui.i18n import UiLanguage, normalize_language, translate


@dataclass
class CommandContext:
    agent: Any
    settings: Any
    manager: Any


@dataclass
class CommandResult:
    ok: bool
    message: str = ""
    should_exit: bool = False
    open_modal: str = ""
    data: object | None = None


@dataclass(frozen=True)
class SlashSubcommandSpec:
    """Help metadata for one command mode or subcommand."""

    name: str
    usage: str
    description: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlashCommandSpec:
    """Read-only metadata shared by command help and TUI suggestions."""

    name: str
    aliases: tuple[str, ...] = ()
    usage: str = ""
    description: str = ""
    category: str = "general"
    details: tuple[str, ...] = ()
    subcommands: tuple[SlashSubcommandSpec, ...] = ()
    examples: tuple[str, ...] = ()
    options: tuple[str, ...] = ()


# Keep this tuple in presentation order.  It is intentionally metadata only;
# command execution remains in CommandService and the legacy compatibility
# helpers in routivus.cli.app.
SLASH_COMMANDS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec(
        "/plan",
        usage="/plan <任务>",
        description="生成、审阅并执行计划",
        category="workflow",
        details=("先生成任务依赖图，审阅通过后再按依赖轮次执行。",),
        examples=("/plan 检查项目配置并修复测试",),
    ),
    SlashCommandSpec(
        "/team",
        usage="/team <任务>",
        description="使用多 Agent 协作完成复杂任务",
        category="workflow",
        details=(
            "由 Supervisor 调度隔离上下文的 Worker，并对任务结果进行证据化审查。",
            "Reviewer 审查失败且缺少修复范围时，任务会进入 needs_input。",
            "用户确认允许修改的文件范围后，可以恢复 Repairer，不会重新执行原任务。",
            "Repairer 只允许修改明确声明的 write scope，不能把只读范围升级为写入权限。",
        ),
        subcommands=(
            SlashSubcommandSpec(
                "run",
                "/team <任务>",
                "创建并执行一个 Team 任务",
            ),
            SlashSubcommandSpec(
                "resume",
                "/team resume <任务ID> --write-scope <范围>",
                "确认写入范围后恢复暂停的 Repairer",
                options=("--write-scope",),
            ),
        ),
        examples=(
            "/team 实现一个带测试的登录模块",
            "/team resume t4 --write-scope routivus/auth/*.py",
        ),
    ),
    SlashCommandSpec(
        "/model",
        usage="/model [list|<model-name>]",
        description="查看当前模型，或在当前 provider 内切换模型",
        category="config",
        details=(
            "不带参数或带 list 时，查看当前模型和可用 provider（含各 provider 模型列表）。",
            "`<model-name>` 在当前 base provider 内切换模型，仅接受该 provider 的模型列表内模型"
            "（含 default_model）。",
            "切换 provider 请用 /provider switch <name>；给 provider 添加模型请用 "
            "/provider <name> model <model>。",
        ),
        subcommands=(
            SlashSubcommandSpec("list", "/model", "查看当前模型和可用 provider"),
            SlashSubcommandSpec("model", "/model <model-name>", "在当前 provider 内切换模型"),
        ),
        examples=(
            "/model",
            "/model list",
            "/model deepseek-chat",
        ),
    ),
    SlashCommandSpec(
        "/config",
        usage="/config get|set ...",
        description="查看或修改配置",
        category="config",
        details=(
            "不带参数时查看当前生效配置，API Key 会脱敏。",
            "`set` 的 value 当前按单个空格分隔参数解析。",
        ),
        subcommands=(
            SlashSubcommandSpec("overview", "/config", "查看当前生效配置"),
            SlashSubcommandSpec("list", "/config list", "查看 provider 能力列表"),
            SlashSubcommandSpec("get", "/config get <key>", "查看指定配置项"),
            SlashSubcommandSpec("set", "/config set <key> <value>", "修改并持久化配置"),
        ),
        examples=(
            "/config",
            "/config list",
            "/config get active_provider",
            "/config set active_model gpt-4o",
        ),
    ),
    SlashCommandSpec(
        "/provider",
        usage="/provider [list|add|show|set|switch|key|remove|<name> model ...]",
        description="在界面内管理 provider（增删改查、切 base、写 Key、维护模型列表）",
        category="config",
        details=(
            "全程界面配置即可，无需手改文件：provider 定义、API Key 与模型列表一律写入 config.json。",
            "add 参数齐全即直接执行；缺省参数会提示所需字段。",
            "给某 provider 加/删模型：/provider <name> model <model>；删除 /provider <name> model rm <model>。",
            "remove 与覆盖已有 Key 需加 --yes 确认；不能删除当前 base provider。",
        ),
        subcommands=(
            SlashSubcommandSpec("list", "/provider", "列出所有 provider（默认/模型列表、是否 base 与来源层）"),
            SlashSubcommandSpec("add", "/provider add <name> <api_base> [--model M] [--label L] [--key K] [--set-base]", "新增一个 provider"),
            SlashSubcommandSpec("show", "/provider show <name>", "查看单个 provider（Key 脱敏、含模型列表）"),
            SlashSubcommandSpec("set", "/provider set <name> <field> <value>", "改单一字段：api_base|default_model|display_name"),
            SlashSubcommandSpec("switch", "/provider switch <name> [model]", "切换 base provider"),
            SlashSubcommandSpec("key", "/provider key <name> <KEY> [--yes]", "写入/覆盖 API Key 到 config.json"),
            SlashSubcommandSpec("remove", "/provider remove <name> [--yes]", "删除 provider（需 --yes；base 不可删）"),
            SlashSubcommandSpec("model", "/provider <name> model <model>", "给某 provider 添加模型到列表"),
            SlashSubcommandSpec("model rm", "/provider <name> model rm <model>", "从某 provider 的模型列表移除模型"),
        ),
        examples=(
            "/provider",
            "/provider add myproxy https://gateway.my.com/v1 --model deepseek-v4 -k sk_x --set-base",
            "/provider myproxy model deepseek-r1",
            "/provider myproxy model rm deepseek-r1",
            "/provider switch deepseek",
            "/provider show myproxy",
        ),
    ),
    SlashCommandSpec(
        "/tier",
        usage="/tier [list|show|set|clear]",
        description="配置 SmartRouter 档位（provider/model，写入 config.json）",
        category="config",
        details=(
            "档位固定为 Basic/Enhanced/Superior/Ultimate 四档。",
            "set 缺省 model 时取该 provider 的 default_model；宽松校验："
            "provider 需存在，model 非空且无非法字符即可。",
        ),
        subcommands=(
            SlashSubcommandSpec("list", "/tier", "列出四档 provider/model（未配回落主动 active）"),
            SlashSubcommandSpec("show", "/tier show <tier>", "查看单个档位"),
            SlashSubcommandSpec("set", "/tier set <tier> <provider> [model]", "设置档位 provider/model"),
            SlashSubcommandSpec("clear", "/tier clear <tier>", "清空档位，回落到手动 active"),
        ),
        examples=(
            "/tier",
            "/tier list",
            "/tier set Basic deepseek deepseek-chat",
            "/tier set Ultimate deepseek",
            "/tier clear Superior",
            "/tier show Enhanced",
        ),
    ),
    SlashCommandSpec(
        "/path",
        usage="/path [add]",
        description="查看或配置 routivus 命令目录的 PATH",
        category="config",
        details=(
            "不带参数时显示命令目录、命令文件、当前会话 PATH 与持久化 PATH 状态。",
            "add 把命令目录写入用户级持久 PATH（幂等），写入后重开终端即可直接运行 routivus。",
            "启动时的自动自愈可用环境变量 ROUTIVUS_AUTO_PATH=0 关闭。",
        ),
        subcommands=(
            SlashSubcommandSpec("add", "/path add", "把命令目录写入持久 PATH"),
        ),
        examples=(
            "/path",
            "/path add",
        ),
    ),
    SlashCommandSpec(
        "/mcp",
        usage="/mcp status|restart|logs|enable|disable|resources",
        description="管理 MCP Server",
        category="config",
        details=("省略子命令时默认查看 Server 状态。",),
        subcommands=(
            SlashSubcommandSpec("status", "/mcp status", "查看 Server、工具和资源状态"),
            SlashSubcommandSpec("restart", "/mcp restart <server>", "重启并重新发现指定 Server"),
            SlashSubcommandSpec("logs", "/mcp logs <server>", "查看指定 Server 的脱敏日志"),
            SlashSubcommandSpec("enable", "/mcp enable <server>", "启用指定 Server"),
            SlashSubcommandSpec("disable", "/mcp disable <server>", "禁用指定 Server"),
            SlashSubcommandSpec("resources", "/mcp resources [server]", "查看全部或指定 Server 的 resources"),
        ),
        examples=(
            "/mcp status",
            "/mcp restart local",
            "/mcp logs local",
            "/mcp resources",
        ),
    ),
    SlashCommandSpec(
        "/web",
        usage="/web status|providers|search|fetch",
        description="查看或使用只读联网能力",
        category="config",
        details=(
            "搜索和抓取均为只读能力；搜索需要配置 provider，抓取接受公开 HTTP(S) URL。",
            "`search` 和 `fetch` 的参数分别作为 query 和 URL 传递。",
        ),
        subcommands=(
            SlashSubcommandSpec("status", "/web status", "查看联网开关、provider 和抓取限制"),
            SlashSubcommandSpec("providers", "/web providers", "查看搜索 provider 配置状态"),
            SlashSubcommandSpec("search", "/web search <query>", "搜索公开互联网"),
            SlashSubcommandSpec("fetch", "/web fetch <url>", "抓取公开网页正文"),
        ),
        examples=(
            "/web status",
            "/web providers",
            "/web search Python 3.13 新特性",
            "/web fetch https://www.example.com/",
        ),
    ),
    SlashCommandSpec(
        "/skill",
        usage="/skill list|load|enable|disable",
        description="管理任务 Skill",
        category="config",
        details=(
            "Skill 是本地任务规范；加载 Skill 不会新增工具权限，也不能覆盖系统提示和安全策略。",
            "`reference` 为可选的指定参考资料路径或名称。",
        ),
        subcommands=(
            SlashSubcommandSpec("list", "/skill list", "查看可用 Skill 元信息"),
            SlashSubcommandSpec("load", "/skill load <name> [reference ...]", "按需加载 Skill 和参考资料"),
            SlashSubcommandSpec("enable", "/skill enable <name>", "启用指定 Skill"),
            SlashSubcommandSpec("disable", "/skill disable <name>", "禁用指定 Skill"),
        ),
        examples=(
            "/skill list",
            "/skill load code-review",
            "/skill load code-review references/style.md",
            "/skill disable code-review",
        ),
    ),
    SlashCommandSpec(
        "/history",
        usage="/history status|clear",
        description="查看或清理输入历史",
        category="session",
        details=("命令只管理本地输入历史，不显示历史全文，也不影响 Agent 对话和长期记忆。",),
        subcommands=(
            SlashSubcommandSpec("status", "/history status", "查看输入历史状态和数量"),
            SlashSubcommandSpec("clear", "/history clear", "清理当前项目的输入历史"),
        ),
        examples=("/history status", "/history clear"),
    ),
    SlashCommandSpec(
        "/memory",
        usage="/memory list|search|delete|clear",
        description="管理长期记忆",
        category="memory",
        details=(
            "长期记忆按当前项目隔离保存；普通对话不会自动写入长期记忆。",
            "`clear` 会要求确认，`delete` 需要使用记忆条目的数字 ID。",
        ),
        subcommands=(
            SlashSubcommandSpec("list", "/memory list [limit]", "列出长期记忆，可选限制条数"),
            SlashSubcommandSpec("search", "/memory search <关键词>", "按关键词搜索长期记忆"),
            SlashSubcommandSpec("delete", "/memory delete <id>", "删除指定 ID 的长期记忆"),
            SlashSubcommandSpec("clear", "/memory clear", "清空全部长期记忆并请求确认"),
        ),
        examples=(
            "/memory list",
            "/memory list 10",
            "/memory search 发布流程",
            "/memory delete 3",
            "/memory clear",
        ),
    ),
    SlashCommandSpec(
        "/lang",
        aliases=("/language",),
        usage="/lang [en|zh|reset]",
        description="查看或切换界面语言",
        category="session",
        details=(
            "默认语言为 English；切换影响产品 UI/CLI 展示文案，不影响 Agent 对话。",
            "reset 会清除用户级语言偏好并恢复 English。",
        ),
        examples=("/lang", "/lang zh", "/lang en", "/lang reset"),
    ),
    SlashCommandSpec("/help", aliases=("/?",), usage="/help", description="查看命令帮助", category="general"),
    SlashCommandSpec(
        "/init",
        usage="/init",
        description="初始化项目记忆",
        category="memory",
        details=("分析当前项目并生成 Routivus.md 草稿；写入前会请求确认，已有文件不会直接覆盖。",),
        examples=("/init",),
    ),
    SlashCommandSpec(
        "/save",
        usage="/save <内容>",
        description="保存长期记忆",
        category="memory",
        details=("只保存用户明确提供的内容；敏感片段会按现有记忆策略脱敏。",),
        examples=("/save 项目使用 Python 3.13，测试命令是 uv run pytest",),
    ),
    SlashCommandSpec(
        "/hitl",
        usage="/hitl on|off|reset",
        description="管理人工审批开关",
        category="safety",
        details=(
            "HITL 用于危险工具调用的人工审批。关闭后危险操作不再弹出审批，属于高风险模式。",
        ),
        subcommands=(
            SlashSubcommandSpec("status", "/hitl", "查看当前审批开关和本会话放行状态"),
            SlashSubcommandSpec("on", "/hitl on", "开启危险操作审批"),
            SlashSubcommandSpec("off", "/hitl off", "关闭危险操作审批并重置放行状态"),
            SlashSubcommandSpec("reset", "/hitl reset", "清除本会话全部放行状态"),
        ),
        examples=("/hitl", "/hitl on", "/hitl reset"),
    ),
    SlashCommandSpec(
        "/ask",
        aliases=("/ask?",),
        usage="/ask <任务描述>",
        description="让模型动手前先向你确认关键信息（问问题+选选项，可自定义输入）",
        category="session",
        examples=("/ask 重构登录模块",),
    ),
    SlashCommandSpec("/clear", usage="/clear", description="清空当前上下文", category="session", examples=("/clear",)),
    SlashCommandSpec(
        "/smartRouter",
        usage="/smartRouter on|off|status|reset",
        description="智能路由开关：每轮输入自动按档位选模型",
        category="general",
        subcommands=(
            SlashSubcommandSpec("status", "/smartRouter status", "查看开关状态、四档模型配置、校准与自学习规则"),
            SlashSubcommandSpec("on", "/smartRouter on", "开启智能路由"),
            SlashSubcommandSpec("off", "/smartRouter off", "关闭并恢复开启前的手动模型"),
            SlashSubcommandSpec("reset", "/smartRouter reset", "清空校准与自学习规则（feedback.log 保留）"),
        ),
        examples=("/smartRouter", "/smartRouter on", "/smartRouter status", "/smartRouter reset"),
    ),
    SlashCommandSpec(
        "/train",
        usage="/train [<dataset>] [--output <path>] [--yes] [--no-semantic]",
        description="训练 SmartRouter ML 模型（手动触发）",
        category="general",
        details=(
            "缺省只用 feedback.log 累积样本；可给手动标注 JSONL 路径混合训练。",
            "命令输入后需加 --yes 确认才会执行；训练过程实时输出进度日志。",
            "训练跑在独立子进程 tools/train_router.py，不阻塞主界面；产物写 router.lgb。",
            "默认自动探测语义编码器并导入语义列（sem_dim>0）；--no-semantic 关闭则纯 TF-IDF。",
        ),
        options=("--yes", "--output <path>", "--feedback-only", "--no-semantic"),
        examples=("/train", "/train labeled.jsonl --yes", "/train --output router.lgb --yes"),
    ),
    SlashCommandSpec("/cancel", aliases=("/c",), usage="/cancel", description="取消当前任务", category="control", examples=("/cancel",)),
    SlashCommandSpec("/exit", aliases=("/quit",), usage="/exit", description="退出程序", category="control", examples=("/exit",)),
)


def filter_slash_commands(query: str) -> tuple[SlashCommandSpec, ...]:
    """Return stable, prefix-matched command specs for a top-level token."""

    normalized = query.strip().lower()
    if not normalized.startswith("/") or any(char.isspace() for char in normalized):
        return ()

    # An exact alias is an unambiguous command selection. For example, `/c`
    # is the cancel alias even though it is also a prefix of `/config` and
    # `/clear`.
    exact_alias = next(
        (
            spec
            for spec in SLASH_COMMANDS
            if any(alias.lower() == normalized for alias in spec.aliases)
        ),
        None,
    )
    if exact_alias is not None:
        return (exact_alias,)

    matches = [
        (index, spec)
        for index, spec in enumerate(SLASH_COMMANDS)
        if spec.name.lower().startswith(normalized)
        or any(alias.lower().startswith(normalized) for alias in spec.aliases)
    ]
    matches.sort(
        key=lambda pair: (
            0
            if pair[1].name.lower() == normalized
            or any(alias.lower() == normalized for alias in pair[1].aliases)
            else 1,
            pair[0],
        )
    )
    return tuple(spec for _, spec in matches)


class CommandService:
    """Execute slash commands without knowing anything about Textual."""

    def __init__(self, context: CommandContext, log_sink: callable | None = None) -> None:
        self.context = context
        # 可选的实时日志通道：由 TUI 传入，把训练/长流程的每一行即时上屏。
        self.log_sink = log_sink

    async def execute(self, raw: str) -> CommandResult:
        raw = raw.strip()
        language = normalize_language(getattr(self.context.settings, "ui_language", "zh"))
        if not raw:
            return CommandResult(ok=False, message=translate(language, "ui.command.empty"))
        if raw.lower() in ("/cancel", "/c"):
            return CommandResult(ok=True, message=translate(language, "ui.command.cancel_requested"))

        parts = raw.split(maxsplit=1)
        if parts[0].lower() in ("/help", "/?"):
            from routivus.cli.help import format_command_help, format_help

            query = parts[1].strip() if len(parts) > 1 else ""
            return CommandResult(
                ok=True,
                message=(
                    format_command_help(query, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
                    if query else format_help(language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
                ),
            )

        if parts[0].lower() in ("/lang", "/language"):
            return _execute_language_command(self.context.settings, self.context.manager, raw)

        if parts[0].lower() == "/provider":
            message, ok = execute_provider_command(self.context.manager, self.context.settings, raw, language=language)
            if ok and self.context.agent is not None:
                # CommandService 路径：/provider 变更后热同步运行中 client
                #（配好即用、无需重启）。
                from routivus.service.commands import _reapply_active

                _reapply_active(self.context.agent, self.context.settings, self.context.manager)
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/tier":
            message, ok = execute_tier_command(self.context.manager, self.context.settings, raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/path":
            message, ok = execute_path_command(raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/train":
            return await execute_train_command(raw, log_sink=self.log_sink, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
        if parts[0].lower() == "/mcp":
            message, ok = await execute_mcp_command(self.context.agent, raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/web":
            message, ok = await execute_web_command(self.context.agent, raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/skill":
            message, ok = await execute_skill_command(self.context.agent, raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)
        if parts[0].lower() == "/history":
            message, ok = await execute_history_command(self.context.agent, raw, language=normalize_language(getattr(self.context.settings, "ui_language", "zh")))
            return CommandResult(ok=ok, message=message)

        # 未显式处理的命令（/clear /model /smartrouter /config /hitl /save /memory
        # /exit 等）交由 service 层命令处理器分发。
        from routivus.service.commands import handle_service_command

        cmd = raw.split(maxsplit=1)[0].lower()
        if cmd == "/init":
            # Generation and confirmation are UI concerns.  The controller
            # handles this command separately so a TUI can show a modal.
            return CommandResult(ok=True, open_modal="init", message=translate(language, "ui.command.init_prepare"))

        message, should_exit = handle_service_command(
            self.context.agent, self.context.settings, self.context.manager, raw
        )
        if message is None:
            message = ""
        unknown = message.startswith(("未知命令", "Unknown command"))
        return CommandResult(ok=not unknown, message=message, should_exit=should_exit)


def _execute_language_command(settings: Any, manager: Any, raw: str) -> CommandResult:
    """Handle the UI-only language preference without touching the Agent."""
    parts = raw.split()
    current = normalize_language(getattr(settings, "ui_language", "en"))
    display_language = normalize_language(getattr(settings, "ui_language", "en"))
    if len(parts) == 1:
        source = "default"
        if hasattr(manager, "ui_language_source"):
            source = manager.ui_language_source()
        return CommandResult(
            ok=True,
            message=(
                f"{translate(display_language, 'ui.command.language_status', name=current, source=source)}\n"
                f"{translate(display_language, 'ui.command.available_languages')}\n"
                f"{translate(display_language, 'ui.command.language_usage')}"
            ),
            data={"ui_language": current, "persisted": True},
        )
    if len(parts) != 2 or parts[1].lower() not in {"en", "zh", "reset"}:
        return CommandResult(
            ok=False,
            message=translate(display_language, "ui.command.language_usage"),
            data={"ui_language": current, "persisted": False},
        )

    requested = parts[1].lower()
    language = "en" if requested == "reset" else requested
    persisted = True
    try:
        if requested == "reset":
            manager.reset_ui_language()
        else:
            manager.set_ui_language(language)
    except (AttributeError, OSError, ValueError) as exc:
        # A read-only or broken config directory must not prevent a session
        # from changing its presentation language.
        persisted = False
        warning = str(exc)
    else:
        warning = ""

    settings.ui_language = language
    action = "reset" if requested == "reset" else "changed"
    message_key = "ui.command.language_reset" if requested == "reset" else "ui.command.language_changed"
    return CommandResult(
        ok=True,
        message=translate(language, message_key, name=language) + (translate(language, "ui.command.session_only", error=warning) if warning else ""),
        data={"ui_language": language, "persisted": persisted},
    )


async def execute_mcp_command(agent: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """Execute the shared asynchronous /mcp command group."""
    manager = getattr(agent, "mcp_manager", None)
    if manager is None:
        return translate(language, "ui.command.mcp_uninitialized"), False
    await manager.ensure_started()
    parts = raw.split()
    sub = parts[1].lower() if len(parts) > 1 else "status"
    name = parts[2] if len(parts) > 2 else ""
    if sub == "status":
        return manager.format_status(), True
    if sub == "resources":
        return manager.format_resources(name or None), True
    if sub == "logs":
        if not name:
            return translate(language, "ui.command.mcp_usage", sub="logs"), False
        return manager.logs(name), name in {item.name for item in manager.snapshots()}
    if sub == "restart":
        if not name:
            return translate(language, "ui.command.mcp_usage", sub="restart"), False
        ok = await manager.restart(name)
        return (translate(language, "ui.command.mcp_restarted", server=name) if ok else translate(language, "ui.command.mcp_restart_failed", server=name)), ok
    if sub in {"enable", "disable"}:
        if not name:
            return translate(language, "ui.command.mcp_usage", sub=sub), False
        enabled = sub == "enable"
        ok = await manager.set_enabled(name, enabled)
        if ok:
            return translate(language, "ui.command.mcp_enabled" if enabled else "ui.command.mcp_disabled", server=name), True
        return translate(language, "ui.command.mcp_action_failed", server=name, action=sub), False
    return translate(language, "ui.command.mcp_full_usage"), False


async def execute_web_command(agent: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """Shared read-only Web command semantics for inline and TUI."""
    config = getattr(agent, "web_config", None)
    parts = raw.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if config is None:
        return translate(language, "ui.command.web_uninitialized"), False
    if sub in {"status", ""}:
        search = config.search
        configured = bool(search.api_base and (search.api_key or search.provider == "searxng"))
        return (f"Web: {translate(language, 'ui.command.enabled' if config.enabled else 'ui.command.disabled')}\n"
                f"search provider: {search.provider} ({translate(language, 'ui.command.configured' if configured else 'ui.command.not_configured')})\n"
                f"fetch: timeout={config.fetch.timeout}s, max={config.fetch.max_response_bytes} bytes, "
                f"chars={config.fetch.max_chars}, redirects={config.fetch.max_redirects}"), True
    if sub == "providers":
        lines = []
        for name in ("zhipu", "serpapi", "searxng"):
            data = config.providers.get(name, {})
            active = name == config.search.provider
            has_key = bool((config.search.api_key if active else None) or data.get("api_key"))
            has_url = bool((config.search.api_base if active else None) or data.get("api_base") or data.get("url"))
            lines.append(f"{name}: {translate(language, 'ui.command.configured' if has_url and (has_key or name == 'searxng') else 'ui.command.not_configured')}")
        return "\n".join(lines), True
    if sub == "search":
        if len(parts) < 3 or not parts[2].strip():
            return translate(language, "ui.command.web_usage", sub="search"), False
        service = getattr(agent, "web_search", None)
        if service is None:
            return translate(language, "ui.command.web_search_unavailable"), False
        ok, output = await service.search_tool({"query": parts[2].strip()})
        return output, ok
    if sub == "fetch":
        if len(parts) < 3 or not parts[2].strip():
            return translate(language, "ui.command.web_usage", sub="fetch"), False
        service = getattr(agent, "web_fetch", None)
        if service is None:
            return translate(language, "ui.command.web_fetch_unavailable"), False
        ok, output = await service.fetch_tool({"url": parts[2].strip()})
        return output, ok
    return translate(language, "ui.command.web_full_usage"), False


async def execute_skill_command(agent: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """Shared read-only Skill management command semantics."""
    registry = getattr(agent, "skill_registry", None)
    if registry is None or not registry.config.enabled:
        return translate(language, "ui.command.skill_disabled"), False
    parts = raw.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"
    if sub == "list":
        registry.reload()
        return registry.format_list(), True
    if sub == "load":
        if len(parts) < 3:
            return translate(language, "ui.command.skill_load_usage"), False
        ok, output = registry.manual_load(parts[2], tuple(parts[3:]))
        return output, ok
    if sub in {"enable", "disable"}:
        if len(parts) < 3:
            return translate(language, "ui.command.skill_usage", sub=sub), False
        enabled = sub == "enable"
        ok = registry.set_enabled(parts[2], enabled)
        if ok:
            return translate(language, "ui.command.skill_enabled" if enabled else "ui.command.skill_disabled_item", name=parts[2]), True
        return translate(language, "ui.command.skill_change_failed", name=parts[2]), False
    return translate(language, "ui.command.skill_full_usage"), False


async def execute_history_command(agent: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """Show or explicitly clear local input history without exposing entries."""
    history = getattr(agent, "input_history", None)
    if history is None:
        return translate(language, "ui.command.history_uninitialized"), False
    parts = raw.split()
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub in {"status", ""}:
        return history.status(), True
    if sub == "clear":
        count = history.clear(persistent=True)
        return translate(language, "ui.command.history_cleared", count=count), True
    return translate(language, "ui.command.history_usage"), False


# ---------------------------------------------------------------------------
# /provider 命令（共享给 inline 与 TUI；确定性实现，确认动作走 --yes）
# ---------------------------------------------------------------------------

def _provider_service(manager: Any, settings: Any, language: UiLanguage = "zh") -> Any:
    from routivus.config.provider_service import ProviderConfigService

    return ProviderConfigService(manager, settings, language=language)


def _provider_usage(sub: str, language: UiLanguage = "zh") -> str:
    usage = {
        "list": "/provider",
        "add": "/provider add <name> <api_base> [--model M] [--label L] [--key K] [--set-base]",
        "show": "/provider show <name>",
        "set": "/provider set <name> <field> <value>  （field: api_base|default_model|display_name）",
        "switch": "/provider switch <name> [model]",
        "key": "/provider key <name> <KEY> [--yes]",
        "remove": "/provider remove <name> [--yes]",
        "model": "/provider <name> model <model> 或 /provider <name> model rm <model>",
    }
    if normalize_language(language) == "en":
        return {
            "list": "/provider",
            "add": "/provider add <name> <api_base> [--model M] [--label L] [--key K] [--set-base]",
            "show": "/provider show <name>",
            "set": "/provider set <name> <field> <value> (field: api_base|default_model|display_name)",
            "switch": "/provider switch <name> [model]",
            "key": "/provider key <name> <KEY> [--yes]",
            "remove": "/provider remove <name> [--yes]",
            "model": "/provider <name> model <model> or /provider <name> model rm <model>",
        }.get(sub, "/provider [list|add|show|set|switch|key|remove|<name> model ...]")
    return usage.get(sub, "/provider [list|add|show|set|switch|key|remove|<name> model ...]")


def _consume_flags(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    """把 ``--key k --model m`` 这类选项从 tokens 中拆出；返回 (flags, 剩余位置参数)。"""
    flags: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    alias = {"--label": "--label", "-l": "--label", "--model": "--model", "-m": "--model", "--key": "--key", "-k": "--key"}
    while i < len(tokens):
        tok = tokens[i]
        if tok in alias:
            # --label 可接单个 token（空格分隔暂不拼接多词 display_name）
            if i + 1 < len(tokens):
                flags[alias[tok]] = tokens[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok in ("--set-base",):
            flags["--set-base"] = "1"
            i += 1
            continue
        if tok in ("--yes", "-y"):
            flags["--yes"] = "1"
            i += 1
            continue
        positional.append(tok)
        i += 1
    return flags, positional


def execute_provider_command(manager: Any, settings: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """执行 /provider 子命令，返回 (message, ok)。

    首个参数优先按子命令解析（list/add/show/set/switch/key）；否则若它
    是既有 provider 名，则进入 provider 作用域（model 加/删）。其余报用法。
    """
    parts = raw.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"
    language = normalize_language(language)
    service = _provider_service(manager, settings, language)

    if sub in {"list", ""}:
        return _render_provider_list(service, language), True

    if sub == "add":
        flags, positional = _consume_flags(parts[2:])
        if len(positional) < 2:
            return _provider_usage("add", language), False
        name, api_base = positional[0], positional[1]
        result = service.add(
            name,
            api_base,
            flags.get("--model", ""),
            display_name=flags.get("--label"),
            api_key=flags.get("--key"),
        )
        if not result.ok:
            return result.message, False
        if "--set-base" in flags:
            switch_result = service.switch(name)
            return f"{result.message}\n{switch_result.message}", switch_result.ok
        return result.message, True

    if sub == "show":
        flags, positional = _consume_flags(parts[2:])
        if not positional:
            return _provider_usage("show", language), False
        row = service.get(positional[0])
        if row is None:
            return translate(language, "ui.provider.unknown", name=positional[0]), False
        models = ", ".join(row["models"]) if row["models"] else ("(none; use /provider <name> model <model> to add)" if language == "en" else "（无，可用 /provider <name> model <model> 添加）")
        return "\n".join(
            [
                f"name:        {row['name']}",
                f"display:     {row['display_name']}",
                f"api_base:    {row['api_base']}",
                f"default:     {row['default_model']}",
                f"models:      {models}",
                f"api_key:     {row['api_key_masked']}（config.json）",
                f"key configured: {'✓' if row['has_key'] else '✗'}" if language == "en" else f"key 已配置:    {'✓' if row['has_key'] else '✗'}",
                f"is_base:       {'✓' if row['is_base'] else '✗'}",
                f"config layer:  {row['layer']}" if language == "en" else f"来源层:       {row['layer']}",
            ]
        ), True

    if sub == "switch":
        flags, positional = _consume_flags(parts[2:])
        if not positional:
            return _provider_usage("switch", language), False
        model = positional[1] if len(positional) > 1 else None
        result = service.switch(positional[0], model)
        return result.message, result.ok

    if sub == "set":
        flags, positional = _consume_flags(parts[2:])
        if len(positional) < 3:
            return _provider_usage("set", language), False
        name, field, value = positional[0], positional[1], positional[2]
        result = service.update(name, {field: value})
        return result.message, result.ok

    if sub == "key":
        flags, positional = _consume_flags(parts[2:])
        if len(positional) < 2:
            return _provider_usage("key", language), False
        result = service.set_api_key(positional[0], positional[1], yes="--yes" in flags)
        return result.message, result.ok

    if sub == "remove":
        flags, positional = _consume_flags(parts[2:])
        if not positional:
            return _provider_usage("remove", language), False
        result = service.remove(positional[0], yes="--yes" in flags)
        return result.message, result.ok

    # provider 作用域：/provider <name> model [rm] <model>
    if len(parts) >= 3 and parts[2].lower() == "model":
        tokens = [t.lower() if t.lower() in ("model", "rm") else t for t in parts[3:]]
        if not tokens:
            return _provider_usage("model", language), False
        if tokens[0] == "rm":
            if len(tokens) < 2:
                return _provider_usage("model", language), False
            result = service.remove_model(parts[1], tokens[1])
            return result.message, result.ok
        result = service.add_model(parts[1], tokens[0])
        return result.message, result.ok

    return _provider_usage("", language), False


def _render_provider_list(service: Any, language: UiLanguage = "zh") -> str:
    language = normalize_language(language)
    rows = service.list()
    if not rows:
        return translate(language, "ui.provider.empty")
    lines = ["NAME              DISPLAY        DEFAULT_MODEL   MODELS                                              KEY  BASE  LAYER"]
    for row in rows:
        models = "、".join(row["models"]) if row["models"] else "-"
        lines.append(
            f"{row['name']:<16}"
            f"{row['display_name'][:12]:<12}"
            f"{row['default_model'][:15]:<15}"
            f"{models[:50]:<50}"
            f"{'✓' if row['has_key'] else '✗':^4}"
            f"{'●' if row['is_base'] else ' ':^6}"
            f"{row['layer']}"
        )
    return "\n".join(lines)


def execute_tier_command(manager: Any, settings: Any, raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """执行 /tier 子命令（list/show/set/clear）。"""
    from routivus.config.smart_router_service import (  # 延迟导入避免环
        SmartRouterConfigService,
        _SMART_ROUTER_TIERS,
    )

    language = normalize_language(language)
    parts = raw.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"
    service = SmartRouterConfigService(manager, settings, language=normalize_language(language))

    if sub in {"list", ""}:
        rows = service.list_tiers()
        lines = ["TIER       PROVIDER       MODEL"]
        for row in rows:
            mark = "*" if row["configured"] else "-"
            provider = row["provider"] or ("(fallback active)" if language == "en" else "（回落 active）")
            model = row["model"] or ""
            lines.append(f"{mark} {row['name']:<9}{provider:<14}{model}")
        lines.append(translate(language, "ui.tier.list_mark"))
        return "\n".join(lines), True

    if sub == "show":
        if len(parts) < 3:
            return ("/tier show <tier> (tiers: " + _tiers_join() + ")" if language == "en" else "/tier show <tier>  （tier: " + _tiers_join() + "）"), False
        result, row = service.get_tier(parts[2])
        if not result.ok:
            return result.message, False
        state = translate(language, "ui.tier.configured") if row["provider"] else translate(language, "ui.tier.fallback")
        return translate(language, "ui.tier.show", name=row["name"], provider=row["provider"] or "-", model=row["model"] or "-", status=state), True

    if sub == "set":
        if len(parts) < 4:
            return "/tier set <tier> <provider> [model]", False
        tier = parts[2]
        provider = parts[3]
        model = parts[4] if len(parts) > 4 else None
        result = service.set_tier(tier, provider, model)
        return result.message, result.ok

    if sub == "clear":
        if len(parts) < 3:
            return "/tier clear <tier>", False
        result = service.clear_tier(parts[2])
        return result.message, result.ok

    return _tier_usage(""), False


def _tiers_join() -> str:
    return "/".join(_SMART_ROUTER_TIERS)


def _tier_usage(sub: str) -> str:
    usage = {
        "list": "/tier",
        "show": "/tier show <tier>",
        "set": "/tier set <tier> <provider> [model]",
        "clear": "/tier clear <tier>",
    }
    return usage.get(sub, "/tier [list|show|set|clear]")


def execute_path_command(raw: str, *, language: UiLanguage = "zh") -> tuple[str, bool]:
    """执行 /path：桌面后端不管理终端 PATH，仅提示不可用。"""
    return (
        "PATH not managed by the desktop backend."
        if normalize_language(language) == "en"
        else "桌面后端不管理终端 PATH（path_heal 已随 inline CLI 移除）。"
    ), False


async def execute_train_command(
    raw: str,
    log_sink: callable | None = None,
    *,
    language: UiLanguage = "zh",
) -> CommandResult:
    """执行 /train：不带 --yes 只返回确认提示；带 --yes 才 spawn 训练并流式上报日志。

    log_sink 由 TUI 提供，逐行即时上屏；缺失时日志合并进返回的 message。
    """
    # 延迟导入避免与 app.py 的循环依赖；train 层不 import 训练逻辑（只 spawn 子进程）。
    from routivus.cli.train import (  # noqa: PLC0415
        check_train_deps,
        confirmation_message,
        parse_train_command,
        run_training_async,
    )

    plan, err = parse_train_command(raw, language=language)
    if err:
        return CommandResult(ok=False, message=err)
    dep_err = check_train_deps(language=language)
    if dep_err:
        return CommandResult(ok=False, message=dep_err)
    if not plan.overwrite:
        return CommandResult(ok=False, message=confirmation_message(plan, language=language))

    ok, lines = await run_training_async(plan, log_sink)
    # log_sink 存在时行日志已逐行实时上屏，这里不再重复，只给简短收尾。
    if ok:
        return CommandResult(
            ok=True,
            message=translate(language, "ui.command.train_done") if log_sink else ("\n".join(lines[-3:]) or translate(language, "ui.command.train_done")),
        )
    tail = "\n".join(lines[-6:]) or ("Training failed (no output)" if language == "en" else "训练失败（无输出）")
    return CommandResult(ok=False, message=translate(language, "ui.command.train_failed") if log_sink else f"{translate(language, 'ui.command.train_failed')}：\n{tail}")
