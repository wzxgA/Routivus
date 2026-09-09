"""Programmatic slash-command execution, independent of any terminal UI.

Port of `routivus.cli.app`'s command-handler chain (model / smartRouter / config /
hitl / memory / provider-resync / model-attach) into a UI-free service layer.

All functions here return strings / tuples intended for a frontend to render;
no ``console.print`` or ``prompt_toolkit`` is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from routivus.config.manager import ConfigManager, ProviderNotConfigured, mask_key
from routivus.config.settings import Settings
from routivus.llm.factory import create_client
from routivus.memory.manager import MemoryManager, MemoryUnavailableError
from routivus.router import TIER_NAMES, resolve as resolve_tier
from routivus.tui.i18n import UiLanguage, normalize_language, translate

if TYPE_CHECKING:
    from routivus.agent.react import ReActAgent


def handle_service_command(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, raw: str
) -> tuple[str | None, bool]:
    """Dispatch a slash command against the backend.

    Returns ``(output_message, should_exit)`` mirroring the legacy inline
    handler. ``should_exit=True`` means the frontend should treat the session
    as terminated (e.g. ``/exit`` / ``/quit``).
    """
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    language = normalize_language(getattr(settings, "ui_language", "zh"))

    if cmd in ("/help", "/?"):
        from routivus.cli.help import format_command_help, format_help

        return (format_command_help(arg, language=language) if arg else format_help(language=language)), False
    if cmd in ("/exit", "/quit"):
        return translate(language, "ui.cli.goodbye"), True
    if cmd in ("/cancel", "/c"):
        return translate(language, "ui.command.inline_cancel"), False
    if cmd == "/clear":
        agent.clear()
        return translate(language, "ui.command.context_cleared"), False
    if cmd in ("/lang", "/language"):
        from routivus.cli.commands import _execute_language_command

        result = _execute_language_command(settings, manager, raw)
        return result.message, False
    if cmd == "/model":
        return _cmd_model(agent, settings, manager, arg, language=language), False
    if cmd == "/smartrouter":
        return _cmd_smart_router(agent, settings, manager, arg, language=language), False
    if cmd == "/config":
        return _cmd_config(agent, settings, manager, arg, language=language), False
    if cmd == "/provider":
        from routivus.cli.commands import execute_provider_command

        message, ok = execute_provider_command(
            manager, settings, raw, language=normalize_language(getattr(settings, "ui_language", "zh"))
        )
        if ok:
            _reapply_active(agent, settings, manager)
        return message, False
    if cmd == "/hitl":
        return _cmd_hitl(agent, arg, language=language), False
    if cmd in ("/save", "/memory"):
        return _cmd_memory_sync(agent, cmd, arg, language=language), False
    if cmd == "/mcp":
        mcp = getattr(agent, "mcp_manager", None)
        return (mcp.format_status() if mcp is not None else translate(language, "ui.command.mcp_uninitialized")), False

    from routivus.cli.commands import SLASH_COMMANDS

    names = " ".join(spec.name for spec in SLASH_COMMANDS)
    return translate(language, "ui.command.unknown", command=cmd, commands=names), False


def _memory_manager(agent: ReActAgent) -> MemoryManager | None:
    return getattr(agent, "memory_manager", None)


def _format_memory_entries(entries) -> str:
    if not entries:
        return "没有找到长期记忆。"
    lines = []
    for entry in entries:
        timestamp = entry.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        preview = " ".join(entry.content.split())
        if len(preview) > 120:
            preview = preview[:120] + " ..."
        lines.append(f"#{entry.id}  {timestamp}  {preview}")
    return "\n".join(lines)


def _cmd_memory_sync(agent: ReActAgent, cmd: str, arg: str, *, language: UiLanguage = "zh") -> str:
    memory = _memory_manager(agent)
    if memory is None:
        return translate(language, "ui.memory.uninitialized")
    try:
        if cmd == "/save":
            if not arg:
                return translate(language, "ui.memory.save_usage")
            entry, created, redacted = memory.save(arg)
            action = "已保存" if created else "已存在，已刷新时间"
            suffix = "（敏感片段已脱敏）" if redacted else ""
            return translate(language, "ui.memory.saved" if created else "ui.memory.refreshed", id=entry.id, suffix=suffix)

        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "list":
            limit = 20
            if rest:
                try:
                    limit = int(rest)
                except ValueError:
                    return translate(language, "ui.memory.list_usage")
            return _format_memory_entries(memory.list(limit))
        if sub == "search":
            if not rest:
                return translate(language, "ui.memory.search_usage")
            return _format_memory_entries(memory.search(rest))
        if sub == "delete":
            try:
                memory_id = int(rest)
            except ValueError:
                return translate(language, "ui.memory.delete_usage")
            return translate(language, "ui.memory.deleted", id=memory_id) if memory.delete(memory_id) else translate(language, "ui.memory.not_found", id=memory_id)
        if sub == "clear":
            return translate(language, "ui.memory.clear_needs_confirmation")
        return translate(language, "ui.memory.usage")
    except (MemoryUnavailableError, OSError, ValueError) as exc:
        return translate(language, "ui.memory.failed", error=exc)


def _cmd_hitl(agent: ReActAgent, arg: str, *, language: UiLanguage = "zh") -> str:
    policy = getattr(agent, "approval_policy", None)
    if policy is None:
        return translate(language, "ui.hitl.uninitialized")
    sub = arg.split()[0].lower() if arg.split() else ""
    if sub == "on":
        policy.set_enabled(True)
        return translate(language, "ui.hitl.enabled")
    if sub == "off":
        policy.set_enabled(False)
        policy.reset_session()
        return translate(language, "ui.hitl.disabled")
    if sub == "reset":
        policy.reset_session()
        return translate(language, "ui.hitl.reset")
    status = translate(language, "ui.tier.enabled" if policy.enabled else "ui.tier.disabled")
    allow_all = "Yes" if policy.session_allow_all and language == "en" else "No" if language == "en" else "是" if policy.session_allow_all else "否"
    return translate(language, "ui.hitl.status", status=status, allow_all=allow_all)


def _cmd_model(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    """/model：list 查看；否则在当前 base provider 内切模型（限模型列表内）。"""
    if not arg or arg.strip().lower() == "list":
        return _model_catalog(manager, language=language)

    model = arg.strip()
    provider = manager.resolve_provider(settings.provider) if settings.provider else None
    if provider is None:
        return translate(language, "ui.model.no_provider", catalog=_model_catalog(manager, language=language))

    allowed = [provider.default_model, *provider.models]
    if model not in allowed:
        available = ", ".join(allowed) if language == "en" else "、".join(allowed) if allowed else "（无）"
        return translate(language, "ui.model.not_allowed", model=model, provider=provider.name, available=available)

    result = _switch(agent, settings, manager, provider.name, model, language=language)

    # 手动优先接管：/model 切换成功即持久化并关闭 SmartRouter 清除快照
    if result == translate(language, "ui.provider.switched", provider=provider.display_name, model=model):
        manager.set_config_value("active_model", model)
        _disable_smart_router(settings, manager)
        return result + translate(language, "ui.model.switched_router_off")
    return result


def _cmd_smart_router(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    """处理 /smartRouter on|off|status。default status。"""
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "status"

    if sub in ("on", "enable"):
        if settings.smart_router_enabled:
            return translate(language, "ui.router.already_enabled")
        settings.smart_router_saved = (settings.provider, settings.model)
        settings.smart_router_enabled = True
        manager.set_smart_router_enabled(True)
        return translate(language, "ui.router.enabled_detail", provider=settings.provider, model=settings.model)

    if sub in ("off", "disable"):
        if not settings.smart_router_enabled:
            return translate(language, "ui.router.already_disabled")
        settings.smart_router_enabled = False
        manager.set_smart_router_enabled(False)
        saved = settings.smart_router_saved
        settings.smart_router_saved = None
        if saved:
            _switch(agent, settings, manager, saved[0], saved[1], language=language)
        return translate(language, "ui.router.disabled_detail")

    if sub in ("reset", "clear"):
        from routivus.adaptive.store import reset_adaptive_data
        from routivus.adaptive.calibrate import recalibrate
        from routivus.adaptive.learned_rules import re_learn
        from routivus.router.postprocess import Hysteresis
        from routivus.router.ml_router import MLRouter

        removed = reset_adaptive_data()
        # 重建内存共享状态，使 reset 立即生效（不再用旧校准/规则）
        agent._smart_calibration = recalibrate()
        agent._smart_learned = re_learn()
        agent._smart_hysteresis = Hysteresis()
        from routivus.router.semantic import load_semantic_encoder

        agent._smart_ml = MLRouter(semantic=load_semantic_encoder())  # 与主循环一致
        detail = ", ".join(removed) if removed and language == "en" else "、".join(removed) if removed else ("(nothing to clear this round)" if language == "en" else "（本轮无可清除项）")
        return translate(language, "ui.router.reset", detail=detail)

    if sub == "status" or arg.strip() in ("status", ""):
        lines = [translate(language, "ui.router.status", status="Enabled" if settings.smart_router_enabled else "Disabled" if language == "en" else "开启" if settings.smart_router_enabled else "关闭")]
        cfg = manager.smart_router_config()
        tiers = cfg.get("tiers") or {}
        for idx, name in enumerate(TIER_NAMES):
            target = resolve_tier(idx, settings.provider, settings.model, tiers, manager)
            raw_entry = tiers.get(name)
            if target.configured:
                mark = "OK"
            else:
                mark = "(x)" if raw_entry else "-"
            lines.append(translate(language, "ui.router.tier_line", name=name, provider=target.provider, model=target.model, mark=mark))
        lines.append(translate(language, "ui.router.legend"))
        # 校准状态：直接聚合 feedback.log 展示实时样本
        from routivus.adaptive.calibrate import aggregate
        from routivus.adaptive.feedback import read_feedback
        from routivus.adaptive.learned_rules import load_learned_rules, rule_hit_stats

        records = read_feedback()
        cal = aggregate(records)
        parts = [f"{name}={cal.samples[i]:g}" for i, name in enumerate(TIER_NAMES)]
        lines.append(translate(language, "ui.router.calibration", values=" ".join(parts)))
        bias_parts = [
            f"{name}={cal.bias[i]:+.2f}" for i, name in enumerate(TIER_NAMES)
        ]
        lines.append(translate(language, "ui.router.bias", values=" ".join(bias_parts), threshold=cal.threshold_adjust))
        # 自学习规则：规则数量与在 feedback.log 上的命中情况
        rules = getattr(agent, "_smart_learned", load_learned_rules())
        stats = rule_hit_stats(records, rules)
        lines.append(
            translate(language, "ui.router.learned", count=stats['rule_count'], hits=stats['hit_records'], samples=stats['sample_records'])
        )
        for pr in stats["per_rule"]:
            pred = "、".join(f"{k}{v:g}" for k, v in pr["predicate"].items())
            lines.append(
                translate(language, "ui.router.rule", predicate=pred, action=pr['action'], confidence=pr['confidence'], support=pr['support'], hits=pr['hits'])
            )
        # ML 精判：产物可用性观测；缺失/缺依赖时显示离线
        ml = getattr(agent, "_smart_ml", None)
        if ml is not None:
            if ml.available:
                n = ml.n_samples
                suffix = f", {n} samples" if n is not None and language == "en" else f"，样本 {n}" if n is not None else ""
                lines.append(translate(language, "ui.router.ml_available", suffix=suffix, dim=ml.sem_dim))
            else:
                lines.append(translate(language, "ui.router.ml_offline"))
            # 语义通道：可用性/产物/耗时/有效样本 观测
            sem = ml.semantic
            if sem is not None:
                if sem.available:
                    lines.append(
                        translate(language, "ui.router.semantic_available", dim=sem.dim, calls=sem.calls, avg=sem.avg_ms, last=sem.last_ms)
                    )
                elif sem.artifact_exists:
                    lines.append(translate(language, "ui.router.semantic_broken"))
                else:
                    lines.append(translate(language, "ui.router.semantic_missing"))
        return "\n".join(lines)

    return translate(language, "ui.router.usage")


def _disable_smart_router(settings: Settings, manager: ConfigManager) -> None:
    """手动 /model 切换后关闭 SmartRouter 并清除快照（手动优先）。"""
    if not settings.smart_router_enabled:
        return
    settings.smart_router_enabled = False
    settings.smart_router_saved = None
    manager.set_smart_router_enabled(False)


def _attach_model(
    settings: Settings, manager: ConfigManager, agent: ReActAgent,
    provider_name: str, model: str,
) -> str | None:
    """仅重建 agent.llm 与内存配置（provider/model/base），不写回持久化。

    SmartRouter 路由用——避免把自动路由到的模型写进 active_provider/active_model。
    返回错误消息；成功返回 None。
    """
    provider = manager.resolve_provider(provider_name)
    if provider is None:
        return translate(settings.ui_language, "ui.provider.unknown", name=provider_name)
    key = manager.resolve_api_key(provider)
    if not key:
        return (f"Missing {provider.name} api_key configuration; use /provider key {provider.name} <KEY> to write config.json." if normalize_language(settings.ui_language) == "en" else f"缺少 {provider.name} 的 api_key 配置，无法使用。请用 /provider key {provider.name} <KEY> 写入 config.json。")
    agent.llm = create_client(
        manager.resolve_api_base(provider), key, model,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    settings.provider = provider.name
    settings.model = model
    settings.api_base = manager.resolve_api_base(provider)
    settings.api_key = key
    settings.context_window = manager.resolve_window(provider)
    return None


def _model_catalog(manager: ConfigManager, *, language: UiLanguage = "zh") -> str:
    """Render the current model and the available providers catalog."""
    active = manager.active()
    lines = [
        translate(language, "ui.model.current", provider=active.provider_name, model=active.model, window=active.context_window),
        translate(language, "ui.model.providers"),
    ]
    for p in manager.list_providers():
        models = ", ".join(p.models) if p.models else ("(none)" if language == "en" else "（无）")
        cache = "cache" if p.supports_cache else "-"
        vision = "vision" if p.supports_vision else "-"
        lines.append(
            f"  {p.name:<10} default={p.default_model}  models=[{models}]  "
            f"{'window' if language == 'en' else '窗口'} {p.context_window:<6} {cache:<5} {vision}"
        )
    return "\n".join(lines)


def _switch(
    agent: ReActAgent,
    settings: Settings,
    manager: ConfigManager,
    provider_name: str,
    model: str | None,
    *, language: UiLanguage = "zh",
) -> str:
    """切换到指定 provider（可选指定模型）。失败返回错误消息，不改变现状。"""
    provider = manager.resolve_provider(provider_name)
    if provider is None:
        return translate(language, "ui.provider.unknown_available", name=provider_name, available=', '.join(manager.provider_names()))
    key = manager.resolve_api_key(provider)
    if not key:
        return (f"Missing {provider.name} api_key configuration; use /provider key {provider.name} <KEY> to write config.json." if language == "en" else f"缺少 {provider.name} 的 api_key 配置，无法切换到 {provider.name}。请用 /provider key {provider.name} <KEY> 写入 config.json。")
    model = model or provider.default_model

    api_base = manager.resolve_api_base(provider)
    settings.provider = provider.name
    settings.model = model
    settings.api_base = api_base
    settings.api_key = key
    settings.context_window = manager.resolve_window(provider)
    agent.llm = create_client(
        api_base, key, model,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    manager.set_active(provider.name, model)
    return translate(language, "ui.provider.switched", provider=provider.display_name, model=model)


def _reapply_active(
    agent: ReActAgent, settings: Settings, manager: ConfigManager
) -> None:
    """/provider 变更后把运行中 client 同步到当前 base，配好即用、无需重启。

    仅同步到能解析的 base；key/api_base 仍缺时保持原样，由调用期引导兜底。
    """
    try:
        active = manager.active()
    except ProviderNotConfigured:
        return
    msg = _switch(agent, settings, manager, active.provider_name, active.model, language=normalize_language(settings.ui_language))
    # 桌面后端不打印；调用方若需要可自行通过日志通道展示 msg


def _cmd_config(
    agent: ReActAgent, settings: Settings, manager: ConfigManager, arg: str, *, language: UiLanguage = "zh"
) -> str:
    parts = arg.split()
    sub = parts[0].lower() if parts else ""

    if sub == "list":
        header = f"{'provider':<14}{'default model' if language == 'en' else '默认模型':<20}{'window' if language == 'en' else '窗口':<8}cache  vision"
        lines = [header]
        for p in manager.list_providers():
            lines.append(
                f"{p.name:<14}{p.default_model:<20}{p.context_window:<8}"
                f"{'✓' if p.supports_cache else '-'}     "
                f"{'✓' if p.supports_vision else '-'}"
            )
        return "\n".join(lines)

    if sub == "get":
        if len(parts) < 2:
            return translate(language, "ui.config.usage_get")
        value = manager.get_config_value(parts[1])
        return f"{parts[1]} = {value if value is not None else translate(language, 'ui.config.value_unset')}"

    if sub == "set":
        if len(parts) < 3:
            return translate(language, "ui.config.usage_set")
        key, value = parts[1], parts[2]
        if key == "active_provider":
            return _switch(agent, settings, manager, value, None, language=language)
        if key == "active_model":
            return _switch(agent, settings, manager, settings.provider, value, language=language)
        manager.set_config_value(key, value)
        return translate(language, "ui.config.set", config_key=key, value=value, path=manager.user_config_path)

    active = manager.active()
    return translate(
        language,
        "ui.config.overview",
        provider=active.provider_name,
        model=active.model,
        api_base=active.api_base,
        api_key=mask_key(active.api_key),
        window=active.context_window,
    ) + f"\ncache:    {'✓' if active.supports_cache else '-'}    vision: {'✓' if active.supports_vision else '-'}"