"""Pure state types consumed by the Textual widgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from xg.tui.i18n import UiLanguage


TranscriptKind = Literal[
    "user", "assistant", "thinking", "tool_call", "tool_result", "approval",
    "context", "plan", "help", "error", "system", "progress", "agent_group",
]
TranscriptStatus = Literal[
    "streaming", "running", "success", "failed", "cancelled", "done",
]
ProgressKind = Literal["response", "plan", "context"]
QueueItemKind = Literal["task", "plan", "command"]
InspectorView = Literal["session", "plan", "memory", "safety"]
TuiPhase = Literal[
    "idle", "running", "awaiting_approval", "awaiting_plan_review",
    "awaiting_team_input", "awaiting_ask", "error",
]


@dataclass
class TranscriptItem:
    id: str
    kind: TranscriptKind
    progress_kind: ProgressKind = "response"
    text: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_call_id: str = ""
    plan: Any | None = None
    plan_review: bool = False
    tool_ok: bool | None = None
    collapsed: bool = True
    collapsible: bool = False
    user_collapsed: bool | None = None
    diagram_source_visible: bool = False
    streaming: bool = False
    turn_id: str = ""
    trace_id: str = ""
    parent_call_id: str = ""
    status: TranscriptStatus = "done"
    elapsed_ms: int | None = None
    agent_group_id: str = ""


@dataclass
class AgentGroupState:
    """One logical AgentRun rendered inside the shared Team transcript."""

    group_id: str
    team_id: str
    agent_id: str
    role: str
    task_id: str
    task_title: str
    resource_scope_mode: str = "targeted"
    status: str = "pending"
    collapsed: bool = True
    user_toggled: bool = False
    event_count: int = 0
    tool_count: int = 0
    artifact_count: int = 0
    repair_attempt: int = 0
    attempt: int = 0
    effective_steps: int = 0
    failure_category: str = ""
    latest_summary: str = ""
    latest_error: str = ""
    entries: list[TranscriptItem] = field(default_factory=list)


@dataclass(frozen=True)
class QueueItem:
    id: str
    text: str
    kind: QueueItemKind
    status: Literal["queued"] = "queued"


@dataclass(frozen=True)
class UsageSnapshot:
    """Current context estimate plus provider request accounting."""

    estimated_prompt_tokens: int = 0
    context_window: int = 0
    request_token_limit: int = 0
    window_ratio: float = 0.0
    budget_usage_ratio: float = 0.0
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    session_prompt_tokens: int = 0
    session_completion_tokens: int = 0
    session_total_tokens: int = 0
    usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
    compaction_count: int = 0
    last_compaction_before: int = 0
    last_compaction_after: int = 0


@dataclass(frozen=True)
class SessionInspectorSnapshot:
    provider: str = ""
    model: str = ""
    status: str = "idle"


@dataclass(frozen=True)
class PlanTaskSnapshot:
    id: str
    title: str
    status: str = "pending"


@dataclass(frozen=True)
class PlanInspectorSnapshot:
    goal: str = ""
    status: str = "idle"
    current_round: int = 0
    total_rounds: int = 0
    completed_tasks: int = 0
    total_tasks: int = 0
    failure_count: int = 0
    tasks: tuple[PlanTaskSnapshot, ...] = ()


@dataclass(frozen=True)
class MemoryInspectorSnapshot:
    project_root: str = ""
    xg_loaded: bool = False
    xg_local_loaded: bool = False
    warning_count: int = 0
    memory_count: int = 0
    store_available: bool = True
    last_operation: str = ""


@dataclass(frozen=True)
class SafetyInspectorSnapshot:
    hitl_enabled: bool = True
    session_allow_all: bool = False
    approval_status: str = "idle"
    current_tool: str = ""
    current_level: str = ""
    last_decision: str = ""
    last_reason: str = ""
    path_guard_active: bool = True
    command_guard_active: bool = True
    last_rejection: str = ""


@dataclass(frozen=True)
class SmartRouterTierSnapshot:
    """One tier entry rendered on the Header routing row."""

    tier: str
    provider: str
    model: str
    is_active: bool = False
    configured: bool = False


@dataclass(frozen=True)
class SmartRouterSnapshot:
    """SmartRouter routing state consumed by HeaderBar (phase-02).

    Off 态 Header 渲染不读取该快照，因此默认值不影响既有渲染路径。
    """

    enabled: bool = False
    tiers: tuple[SmartRouterTierSnapshot, ...] = ()
    active_tier: str = ""
    # 第 3 期校准后的置信度展示位，本期恒为 None。
    confidence: float | None = None


@dataclass
class InspectorState:
    active_view: InspectorView = "session"
    session: SessionInspectorSnapshot = field(default_factory=SessionInspectorSnapshot)
    plan: PlanInspectorSnapshot = field(default_factory=PlanInspectorSnapshot)
    memory: MemoryInspectorSnapshot = field(default_factory=MemoryInspectorSnapshot)
    safety: SafetyInspectorSnapshot = field(default_factory=SafetyInspectorSnapshot)
    smart_router: SmartRouterSnapshot = field(default_factory=SmartRouterSnapshot)
    provider: str = ""
    model: str = ""
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    # Compatibility fields retained for adapters written before UsageSnapshot.
    context_tokens: int = 0
    context_window: int = 0
    memory_count: int = 0
    hitl_enabled: bool = True
    plan_status: str = "idle"
    batch: str = ""


@dataclass
class TuiState:
    phase: TuiPhase = "idle"
    transcript: list[TranscriptItem] = field(default_factory=list)
    inspector: InspectorState = field(default_factory=InspectorState)
    active_turn_id: str = ""
    active_input: str = ""
    pending_approval: Any | None = None
    pending_plan: Any | None = None
    pending_confirmation: ConfirmationRequest | None = None
    pending_ask: Any | None = None
    team_input_task_id: str = ""
    team_input_category: str = ""
    team_input_message: str = ""
    team_input_scope: list[Any] = field(default_factory=list)
    notification: str = ""
    notification_level: Literal["info", "warning", "error"] = "info"
    plan_tasks: dict[str, str] = field(default_factory=dict)
    agent_groups: dict[str, AgentGroupState] = field(default_factory=dict)
    agent_group_order: list[str] = field(default_factory=list)
    queue: list[QueueItem] = field(default_factory=list)
    # UI preference only; never serialized into Agent messages or memory.
    ui_language: UiLanguage = "en"


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    level: str
    args: dict
    turn_id: str = ""


@dataclass(frozen=True)
class ConfirmationRequest:
    kind: str
    title: str
    body: str
    payload: object | None = None
