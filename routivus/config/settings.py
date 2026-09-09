"""运行时配置快照与加载。

Settings 是解析后的运行时快照（provider / model / key / 窗口等），
由 ConfigManager 按「默认 < 用户级 < 项目级 < 环境变量」合并产出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from routivus.config.manager import ConfigManager, ProviderNotConfigured
from routivus.config.skills import SkillConfigManager
from routivus.tui.i18n import UiLanguage, normalize_language


@dataclass
class Settings:
    """运行时配置快照。provider / model 可被 /model 命令运行时修改。"""

    provider: str = ""
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    context_window: int = 128_000
    tool_steps: int = 20
    # LLM transient request retry policy.
    llm_retry_enabled: bool = True
    llm_max_retries: int = 2
    llm_retry_base_delay: float = 1.0
    llm_retry_max_delay: float = 8.0
    llm_retry_jitter: float = 0.25
    llm_retry_total_timeout: float = 30.0
    llm_respect_retry_after: bool = True
    # token 预算阈值：messages 估算 token 超过 window * budget_ratio 时终止循环
    budget_ratio: float = 0.8
    # 第 5 期：自动压缩时保留的最近完整对话轮次
    context_keep_recent_turns: int = 4
    # 第 5 期：摘要输出动态预留上限
    context_summary_max_tokens: int = 4096
    # 第 5 期：自动注入长期记忆的字符上限
    memory_prompt_max_chars: int = 8000
    # 第 5 期：单个 XG.md/XG.local.md 的读取字符上限
    project_memory_max_chars: int = 32000
    # 工具输出超出该字符数则截断，防止撑爆上下文
    max_tool_output_chars: int = 20_000
    # 并行工具执行并发数（第 3 期）
    max_parallel: int = 4
    # 单工具执行超时（秒，第 3 期）
    tool_timeout: float = 120.0
    # HITL 审批默认开启（第 3 期）
    hitl: bool = True
    # 计划模式（第 4 期）：子任务数上限
    plan_max_subtasks: int = 12
    # 计划模式：每个子任务的最大工具步数
    plan_subtask_steps: int = 10
    # 计划模式：计划级允许的子任务失败数（超出终止剩余批次）
    plan_max_failures: int = 3
    # 第 10 期：Team 协作限制
    team_max_agents: int = 4
    team_max_repairs: int = 2
    # 第 V3 期：Plan/Team 任务自然语言断点续跑
    # 单个任务最多自动恢复次数（防「失败→继续→失败」死循环消耗预算）
    task_max_resumes: int = 3
    # 恢复意图是否用 LLM 识别；关闭后仅用关键词白名单
    resume_intent_llm: bool = True
    # Reviewer 输出协议重试次数；与 API 重试和 Repairer 次数分离。
    team_review_output_retries: int = 1
    # Team Worker role-specific execution budgets. None keeps the legacy fallback.
    team_researcher_steps: int | None = None
    team_reviewer_steps: int | None = None
    team_coder_steps: int | None = None
    team_tester_steps: int | None = None
    team_repairer_steps: int | None = None
    team_synthesizer_steps: int | None = None
    team_max_steps: int = 40
    team_recovery_steps: int = 10
    team_max_recoveries: int = 1
    team_review: bool = True
    # Textual TUI 最大刷新频率（第 6 期）
    tui_refresh_fps: int = 20
    # 第 7 期：MCP 客户端与资源限制
    mcp_enabled: bool = True
    mcp_startup_timeout: float = 15.0
    mcp_request_timeout: float = 120.0
    mcp_shutdown_timeout: float = 5.0
    mcp_max_servers: int = 32
    mcp_max_tools: int = 256
    mcp_max_resources: int = 512
    mcp_max_message_bytes: int = 2_097_152
    mcp_resource_max_chars: int = 32_000
    mcp_resource_total_chars: int = 64_000
    mcp_log_lines: int = 200
    # 第 8 期：只读 Web 能力限制
    web_enabled: bool = True
    web_search_provider: str = "none"
    web_timeout: float = 15.0
    web_max_results: int = 5
    web_max_response_bytes: int = 2_097_152
    web_fetch_max_chars: int = 32_000
    web_max_redirects: int = 5
    web_rate_limit_per_minute: int = 30
    # 第 9 期：Skill 索引和按需加载限制
    skills_enabled: bool = True
    skills_max_index_items: int = 20
    skills_max_index_chars: int = 4_096
    skills_max_chars: int = 32_000
    skills_max_reference_chars: int = 16_000
    skills_max_loaded_chars: int = 64_000
    # 输入历史：只影响本地 Composer/inline 输入，不进入 Agent 上下文
    input_history_enabled: bool = True
    input_history_persist: bool = True
    input_history_max_entries: int = 100
    input_history_max_chars: int = 8_000
    input_history_max_bytes: int = 1_048_576
    extra: dict[str, str] = field(default_factory=dict)
    # Inspector UI language; this does not affect Agent prompts or responses.
    # Directly constructed Settings objects retain the historical Chinese
    # presentation; load_settings() supplies the product default (English)
    # after resolving config. This keeps library/test adapters compatible.
    ui_language: UiLanguage = "zh"
    # SmartRouter（phase-01）：智能路由开关，默认关闭；开启后每轮输入自动路由换档。
    # 路由结果只改内存中的 provider/model，不写回 active_provider/active_model。
    smart_router_enabled: bool = False
    # 开启前的手动模型快照 (provider, model)，/smartRouter off 时恢复；未开启时为 None。
    smart_router_saved: tuple[str, str] | None = None
    # base provider 未配置时置 True（active() 触发 ProviderNotConfigured），供启动提示。
    provider_missing: bool = False
    # v1.1.6 Ask-User 交互询问：模型不确定/需澄清/用户要求反问时停下来提问
    ask_user_enabled: bool = True
    ask_max_fields: int = 5
    ask_max_options: int = 8

    @property
    def token_budget(self) -> int:
        return int(self.context_window * self.budget_ratio)

    def estimate_tokens(self, text: str) -> int:
        """字符近似估算：ASCII 约 4 字符/token，非 ASCII（如中文）约 1 字符/token。"""
        if not text:
            return 0
        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        non_ascii = len(text) - ascii_chars
        return ascii_chars // 4 + non_ascii


def load_settings(manager: ConfigManager | None = None) -> Settings:
    """加载配置并产出运行时快照。"""
    manager = manager or ConfigManager()
    try:
        active = manager.active()
        provider_missing = False
    except ProviderNotConfigured:
        active = None
        provider_missing = True
    skill_config = SkillConfigManager(
        user_dir=manager.user_dir,
        project_root=manager.project_dir.parent,
        env=manager.env,
    ).load()
    return Settings(
        provider=active.provider_name if active else "",
        api_base=active.api_base if active else "",
        api_key=active.api_key if active else "",
        model=active.model if active else "",
        context_window=active.context_window if active else 128_000,
        provider_missing=provider_missing,
        tool_steps=_get_int(manager.env, "XG_TOOL_STEPS", 20),
        llm_retry_enabled=manager.env.get("XG_LLM_RETRY_ENABLED", "on").lower() not in ("off", "0", "false"),
        llm_max_retries=max(0, _get_int(manager.env, "XG_LLM_MAX_RETRIES", 2)),
        llm_retry_base_delay=max(0.0, _get_float(manager.env, "XG_LLM_RETRY_BASE_DELAY", 1.0)),
        llm_retry_max_delay=max(0.0, _get_float(manager.env, "XG_LLM_RETRY_MAX_DELAY", 8.0)),
        llm_retry_jitter=_clamp_float(_get_float(manager.env, "XG_LLM_RETRY_JITTER", 0.25), 0.0, 1.0),
        llm_retry_total_timeout=max(0.0, _get_float(manager.env, "XG_LLM_RETRY_TOTAL_TIMEOUT", 30.0)),
        llm_respect_retry_after=manager.env.get("XG_LLM_RESPECT_RETRY_AFTER", "on").lower() not in ("off", "0", "false"),
        budget_ratio=_clamp_float(_get_float(manager.env, "XG_CONTEXT_BUDGET_RATIO", 0.8), 0.5, 0.9),
        context_keep_recent_turns=max(0, _get_int(manager.env, "XG_CONTEXT_KEEP_RECENT_TURNS", 4)),
        context_summary_max_tokens=max(512, _get_int(manager.env, "XG_CONTEXT_SUMMARY_MAX_TOKENS", 4096)),
        memory_prompt_max_chars=max(256, _get_int(manager.env, "XG_MEMORY_PROMPT_MAX_CHARS", 8000)),
        project_memory_max_chars=max(256, _get_int(manager.env, "XG_PROJECT_MEMORY_MAX_CHARS", 32000)),
        max_parallel=_get_int(manager.env, "XG_MAX_PARALLEL", 4),
        tool_timeout=_get_float(manager.env, "XG_TOOL_TIMEOUT", 120.0),
        hitl=manager.env.get("XG_HITL", "on").lower() not in ("off", "0", "false"),
        plan_max_subtasks=_get_int(manager.env, "XG_PLAN_MAX_SUBTASKS", 12),
        plan_subtask_steps=_get_int(manager.env, "XG_PLAN_SUBTASK_STEPS", 10),
        plan_max_failures=_get_int(manager.env, "XG_PLAN_MAX_FAILURES", 3),
        task_max_resumes=max(1, _get_int(manager.env, "XG_TASK_MAX_RESUMES", 3)),
        resume_intent_llm=manager.env.get("XG_RESUME_INTENT_LLM", "on").lower() not in ("off", "0", "false"),
        team_max_agents=max(1, _get_int(manager.env, "XG_TEAM_MAX_AGENTS", 4)),
        team_max_repairs=max(0, _get_int(manager.env, "XG_TEAM_MAX_REPAIRS", 2)),
        team_review_output_retries=max(0, _get_int(manager.env, "XG_TEAM_REVIEW_OUTPUT_RETRIES", 1)),
        team_researcher_steps=_get_optional_positive_int(manager.env, "XG_TEAM_RESEARCHER_STEPS"),
        team_reviewer_steps=_get_optional_positive_int(manager.env, "XG_TEAM_REVIEWER_STEPS"),
        team_coder_steps=_get_optional_positive_int(manager.env, "XG_TEAM_CODER_STEPS"),
        team_tester_steps=_get_optional_positive_int(manager.env, "XG_TEAM_TESTER_STEPS"),
        team_repairer_steps=_get_optional_positive_int(manager.env, "XG_TEAM_REPAIRER_STEPS"),
        team_synthesizer_steps=_get_optional_positive_int(manager.env, "XG_TEAM_SYNTHESIZER_STEPS"),
        team_max_steps=max(1, _get_int(manager.env, "XG_TEAM_MAX_STEPS", 40)),
        team_recovery_steps=max(1, _get_int(manager.env, "XG_TEAM_RECOVERY_STEPS", 10)),
        team_max_recoveries=max(0, _get_int(manager.env, "XG_TEAM_MAX_RECOVERIES", 1)),
        team_review=manager.env.get("XG_TEAM_REVIEW", "on").lower() not in ("off", "0", "false"),
        tui_refresh_fps=max(5, min(60, _get_int(manager.env, "XG_TUI_REFRESH_FPS", 20))),
        ui_language=normalize_language(manager.get_ui_language()),
        mcp_enabled=manager.env.get("XG_MCP_ENABLED", "on").lower() not in ("off", "0", "false"),
        mcp_startup_timeout=max(0.1, _get_float(manager.env, "XG_MCP_STARTUP_TIMEOUT", 15.0)),
        mcp_request_timeout=max(0.1, _get_float(manager.env, "XG_MCP_REQUEST_TIMEOUT", 120.0)),
        mcp_shutdown_timeout=max(0.1, _get_float(manager.env, "XG_MCP_SHUTDOWN_TIMEOUT", 5.0)),
        mcp_max_servers=max(1, _get_int(manager.env, "XG_MCP_MAX_SERVERS", 32)),
        mcp_max_tools=max(1, _get_int(manager.env, "XG_MCP_MAX_TOOLS", 256)),
        mcp_max_resources=max(1, _get_int(manager.env, "XG_MCP_MAX_RESOURCES", 512)),
        mcp_max_message_bytes=max(1024, _get_int(manager.env, "XG_MCP_MAX_MESSAGE_BYTES", 2_097_152)),
        mcp_resource_max_chars=max(256, _get_int(manager.env, "XG_MCP_RESOURCE_MAX_CHARS", 32_000)),
        mcp_resource_total_chars=max(256, _get_int(manager.env, "XG_MCP_RESOURCE_TOTAL_CHARS", 64_000)),
        mcp_log_lines=max(10, _get_int(manager.env, "XG_MCP_LOG_LINES", 200)),
        web_enabled=manager.env.get("XG_WEB_ENABLED", "on").lower() not in ("off", "0", "false"),
        web_search_provider=manager.env.get("XG_WEB_SEARCH_PROVIDER", "none").lower() or "none",
        web_timeout=max(0.1, _get_float(manager.env, "XG_WEB_TIMEOUT", 15.0)),
        web_max_results=max(1, min(10, _get_int(manager.env, "XG_WEB_MAX_RESULTS", 5))),
        web_max_response_bytes=max(1024, _get_int(manager.env, "XG_WEB_MAX_RESPONSE_BYTES", 2_097_152)),
        web_fetch_max_chars=max(256, _get_int(manager.env, "XG_WEB_FETCH_MAX_CHARS", 32_000)),
        web_max_redirects=max(0, _get_int(manager.env, "XG_WEB_MAX_REDIRECTS", 5)),
        web_rate_limit_per_minute=max(1, _get_int(manager.env, "XG_WEB_RATE_LIMIT_PER_MINUTE", 30)),
        skills_enabled=skill_config.enabled,
        skills_max_index_items=skill_config.max_index_items,
        skills_max_index_chars=skill_config.max_index_chars,
        skills_max_chars=skill_config.max_skill_chars,
        skills_max_reference_chars=skill_config.max_reference_chars,
        skills_max_loaded_chars=skill_config.max_loaded_chars,
        input_history_enabled=manager.env.get("XG_INPUT_HISTORY_ENABLED", "on").lower() not in ("off", "0", "false"),
        input_history_persist=manager.env.get("XG_INPUT_HISTORY_PERSIST", "on").lower() not in ("off", "0", "false"),
        input_history_max_entries=max(1, _get_int(manager.env, "XG_INPUT_HISTORY_MAX_ENTRIES", 100)),
        input_history_max_chars=max(256, _get_int(manager.env, "XG_INPUT_HISTORY_MAX_CHARS", 8_000)),
        input_history_max_bytes=max(4_096, _get_int(manager.env, "XG_INPUT_HISTORY_MAX_BYTES", 1_048_576)),
        smart_router_enabled=bool(manager.smart_router_config().get("enabled", False)),
        ask_user_enabled=manager.env.get("XG_ASK_USER", "on").lower() not in ("off", "0", "false"),
        ask_max_fields=max(1, min(20, _get_int(manager.env, "XG_ASK_MAX_FIELDS", 5))),
        ask_max_options=max(1, min(20, _get_int(manager.env, "XG_ASK_MAX_OPTIONS", 8))),
    )


def _get_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _get_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_optional_positive_int(env: dict[str, str], name: str) -> int | None:
    raw = env.get(name, "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
