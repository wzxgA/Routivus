"""Formatting helpers for the shared slash-command help."""

from __future__ import annotations

from collections.abc import Sequence

from xg.cli.commands import SLASH_COMMANDS, SlashCommandSpec, SlashSubcommandSpec
from xg.tui.i18n import UiLanguage, normalize_language, translate


CATEGORY_LABELS: dict[str, str] = {
    "workflow": "工作流",
    "config": "配置与能力",
    "session": "会话",
    "memory": "记忆",
    "safety": "安全",
    "control": "控制",
    "general": "通用",
}

CATEGORY_ORDER = (
    "workflow",
    "config",
    "session",
    "memory",
    "safety",
    "control",
    "general",
)

CATEGORY_LABELS_EN = {
    "workflow": "Workflow", "config": "Configuration & capabilities",
    "session": "Session", "memory": "Memory", "safety": "Safety",
    "control": "Control", "general": "General",
}

DESCRIPTION_EN = {
    "/plan": "Generate, review, and execute a plan",
    "/team": "Use multiple Agents to complete a complex task",
    "/model": "View or switch models within the current provider",
    "/config": "View or update persisted configuration",
    "/provider": "Manage providers and their model lists",
    "/tier": "Manage SmartRouter model tiers",
    "/path": "Configure the command directory on PATH",
    "/mcp": "Manage MCP Servers, tools, and resources",
    "/web": "View web capabilities or use web search/fetch",
    "/skill": "Manage and load Skills",
    "/history": "Manage input history",
    "/memory": "Manage long-term memory",
    "/lang": "View or switch the UI language",
    "/help": "View command help",
    "/init": "Initialize project memory",
    "/save": "Save a memory entry",
    "/hitl": "Manage dangerous-operation approval",
    "/clear": "Clear the current context",
    "/smartRouter": "Manage SmartRouter routing",
    "/cancel": "Cancel the current task",
    "/exit": "Exit the program",
}

SUBCOMMAND_EN = {
    "list": "List current values",
    "run": "Create and execute a task",
    "resume": "Resume a paused task after confirming its write scope",
    "overview": "View the active configuration",
    "get": "View a configuration value",
    "set": "Change and persist a configuration value",
    "add": "Add a provider or configure PATH",
    "show": "View one provider or tier",
    "switch": "Switch the base provider",
    "key": "Write or replace an API key",
    "remove": "Remove a provider",
    "model": "Add or switch a model",
    "model rm": "Remove a model from a provider",
    "clear": "Clear a tier or memory",
    "status": "View current status",
    "restart": "Restart and rediscover a Server",
    "logs": "View redacted Server logs",
    "enable": "Enable a Server or Skill",
    "disable": "Disable a Server or Skill",
    "resources": "View resources",
    "providers": "View search provider configuration",
    "search": "Search the public internet",
    "fetch": "Fetch a public web page",
    "load": "Load a Skill and its references",
    "on": "Enable the feature",
    "off": "Disable the feature",
    "reset": "Reset temporary or learned state",
}

DETAILS_EN = {
    "/plan": (
        "Generate a dependency graph, review it, then execute tasks by dependency round.",
    ),
    "/team": (
        "A Supervisor schedules isolated Workers and reviews their results with evidence.",
        "A failed review may pause at needs_input until the user confirms a write scope.",
        "A Repairer may only modify its explicitly declared write scope.",
    ),
    "/model": (
        "With no argument or with list, show the current model and available providers.",
        "<model-name> switches models within the current base provider and must be in its model list.",
        "Use /provider switch <name> to switch providers, or /provider <name> model <model> to add a model.",
    ),
    "/config": ("View merged configuration or persist a configuration value.",),
    "/provider": ("Provider API keys are masked in display and are never exposed in help output.",),
    "/tier": ("Configure Basic, Enhanced, Superior, and Ultimate routing tiers.",),
    "/path": ("Configure a persistent PATH entry for the command directory.",),
    "/mcp": ("MCP configuration and logs are handled without exposing secrets.",),
    "/web": ("Web results are external, untrusted data and do not change XG policy.",),
    "/skill": ("Skills are supplementary task instructions and cannot override system or safety rules.",),
    "/history": ("Input history is local to the project and can be cleared independently.",),
    "/memory": ("Long-term memory is project-scoped and clear requires confirmation.",),
    "/lang": ("The preference changes product UI/CLI copy only; it does not change Agent responses.",),
    "/init": ("Analyze the current project and prepare an XG.md draft without overwriting an existing file.",),
    "/save": ("Save the supplied text to project-scoped long-term memory.",),
    "/hitl": ("Dangerous operations remain subject to policy checks even when approval is enabled.",),
    "/clear": ("Clear the current conversation context.",),
    "/smartRouter": ("SmartRouter selects a model tier for each ordinary turn when enabled.",),
    "/cancel": ("Cancel the active task; queued work is handled by the session controller.",),
    "/exit": ("Exit the current session.",),
}

SHORTCUTS = (
    ("Enter", "发送输入"),
    ("↑ / ↓", "浏览输入历史；输入 / 时浏览命令建议"),
    ("Ctrl+C", "取消当前任务"),
    ("Ctrl+L", "清屏"),
    ("Ctrl+R", "显示或隐藏侧栏"),
    ("Ctrl+T", "打开配置面板（Provider 与 SmartRouter）"),
    ("Ctrl+1..4", "切换 Inspector 视图（Session/Plan/Memory/Safety）"),
    ("Ctrl+Tab", "Inspector 视图循环切换（Ctrl+Shift+Tab 反向）"),
    ("Esc", "关闭弹窗、取消当前交互或清除输入"),
)


def _command_key(value: str) -> str:
    """Normalize a command name or alias for exact help lookup."""

    return value.strip().lower().lstrip("/")


def parse_help_command(raw: str) -> str | None:
    """Return the optional help query, or ``None`` for a non-help input."""

    parts = raw.strip().split(maxsplit=1)
    if not parts or parts[0].lower() not in ("/help", "/?"):
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def _aliases_text(spec: SlashCommandSpec, language: UiLanguage = "zh") -> str:
    if not spec.aliases:
        return ""
    if normalize_language(language) == "en":
        return f" (aliases: {', '.join(spec.aliases)})"
    return f"（别名：{'、'.join(spec.aliases)}）"


def _command_lines(commands: Sequence[SlashCommandSpec], language: UiLanguage = "zh") -> list[str]:
    visible = [spec for spec in commands if spec.usage and spec.description]
    if not visible:
        return []
    usage_width = max(len(spec.usage) for spec in visible)
    lines: list[str] = []
    for category in CATEGORY_ORDER:
        grouped = [spec for spec in visible if spec.category == category]
        if not grouped:
            continue
        category_labels = CATEGORY_LABELS_EN if normalize_language(language) == "en" else CATEGORY_LABELS
        lines.append(category_labels.get(category, category))
        for spec in grouped:
            usage = spec.usage.ljust(usage_width)
            desc = DESCRIPTION_EN.get(spec.name, spec.description) if normalize_language(language) == "en" else spec.description
            lines.append(f"  {usage}  {desc}{_aliases_text(spec, language)}")
        lines.append("")

    # Keep custom categories visible even if a future command adds one that
    # is not yet part of the standard presentation order.
    known = set(CATEGORY_ORDER)
    for category in dict.fromkeys(spec.category for spec in visible if spec.category not in known):
        category_labels = CATEGORY_LABELS_EN if normalize_language(language) == "en" else CATEGORY_LABELS
        lines.append(category_labels.get(category, category))
        for spec in visible:
            if spec.category == category:
                usage = spec.usage.ljust(usage_width)
                desc = DESCRIPTION_EN.get(spec.name, spec.description) if normalize_language(language) == "en" else spec.description
                lines.append(f"  {usage}  {desc}{_aliases_text(spec, language)}")
        lines.append("")
    return lines


def format_help(
    commands: Sequence[SlashCommandSpec] = SLASH_COMMANDS,
    *,
    include_shortcuts: bool = True,
    language: UiLanguage = "zh",
) -> str:
    """Return the complete, renderer-neutral slash-command help text."""

    language = normalize_language(language)
    lines = [translate(language, "ui.help.title"), ""]
    lines.extend(_command_lines(commands, language))

    aliases = [
        f"{alias} = {spec.name}"
        for spec in commands
        for alias in spec.aliases
    ]
    if aliases:
        lines.extend([
            translate(language, "ui.help.aliases"),
            f"  {'    '.join(aliases)}", "",
        ])

    if include_shortcuts:
        lines.append(translate(language, "ui.help.shortcuts"))
        shortcuts = SHORTCUTS if language == "zh" else (
            ("Enter", "Send input"),
            ("↑ / ↓", "Browse input history; browse command suggestions after /"),
            ("Ctrl+C", "Cancel the current task"),
            ("Ctrl+L", "Clear the transcript"),
            ("Ctrl+R", "Show or hide the sidebar"),
            ("Ctrl+T", "Open the configuration panel (Provider and SmartRouter)"),
            ("Ctrl+1..4", "Switch Inspector views (Session/Plan/Memory/Safety)"),
            ("Ctrl+Tab", "Cycle Inspector views (reverse with Ctrl+Shift+Tab)"),
            ("Esc", "Close dialogs, cancel interaction, or clear input"),
        )
        shortcut_width = max(len(key) for key, _ in shortcuts)
        lines.extend(
            f"  {key.ljust(shortcut_width)}  {description}"
            for key, description in shortcuts
        )
        lines.append("")

    lines.extend(
        [
            translate(language, "ui.help.hint_suggestions"),
            translate(language, "ui.help.hint_details"),
        ]
    )
    return "\n".join(lines).rstrip()


def format_command_help(
    query: str,
    commands: Sequence[SlashCommandSpec] = SLASH_COMMANDS,
    *,
    language: UiLanguage = "zh",
) -> str:
    """Return help for one command or an actionable unknown-command hint."""

    token = query.strip().split(maxsplit=1)[0] if query.strip() else ""
    normalized = _command_key(token)
    if not normalized:
        return format_help(commands, language=language)

    spec = find_command(token, commands)
    if spec is None:
        if normalize_language(language) == "en":
            return f"{translate(language, 'ui.help.not_found', command=token)} {translate(language, 'ui.help.all')}."
        return f"未找到命令帮助：{token}。输入 /help 查看全部命令。"

    description = DESCRIPTION_EN.get(spec.name, spec.description) if normalize_language(language) == "en" else spec.description
    usage_label = "Usage" if normalize_language(language) == "en" else "用法"
    lines = [f"{spec.name} — {description}", f"{usage_label}：{spec.usage}"]
    if spec.aliases:
        lines.append(f"Aliases: {', '.join(spec.aliases)}" if normalize_language(language) == "en" else f"别名：{'、'.join(spec.aliases)}")
    if spec.details:
        details = DETAILS_EN.get(spec.name, spec.details) if normalize_language(language) == "en" else spec.details
        if normalize_language(language) == "en":
            details = tuple(details)
        lines.extend(["", *details])
    if spec.subcommands:
        lines.extend(["", "Subcommands" if normalize_language(language) == "en" else "子命令"])
        lines.extend(_subcommand_lines(spec.subcommands, language))
    if spec.examples:
        lines.extend(["", "Examples" if normalize_language(language) == "en" else "示例"])
        lines.extend(f"  {example}" for example in spec.examples)
    return "\n".join(lines)


def _subcommand_lines(subcommands: Sequence[SlashSubcommandSpec], language: UiLanguage = "zh") -> list[str]:
    """Format detailed command modes with stable aligned columns."""

    usage_width = max(len(item.usage) for item in subcommands)
    return [
        f"  {item.usage.ljust(usage_width)}  {(SUBCOMMAND_EN.get(item.name, item.description) if normalize_language(language) == 'en' else item.description)}"
        for item in subcommands
    ]


def find_command(
    query: str,
    commands: Sequence[SlashCommandSpec] = SLASH_COMMANDS,
) -> SlashCommandSpec | None:
    """Find a command by its name or alias."""

    normalized = _command_key(query)
    if not normalized:
        return None
    return next(
        (
            item
            for item in commands
            if _command_key(item.name) == normalized
            or any(_command_key(alias) == normalized for alias in item.aliases)
        ),
        None,
    )
