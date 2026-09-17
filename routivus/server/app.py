"""FastAPI application for the Routivus Web Console."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from routivus import __version__
from routivus.config.providers import DEFAULT_MAX_TOKENS_FIELD
from routivus.memory.manager import MemoryManager
from routivus.safety.audit import AuditLogger
from routivus.safety.hitl import ApprovalDecision
from routivus.server.approval import ApprovalBridge
from routivus.server.completions import completion_payload
from routivus.server.config import ServerConfig
from routivus.server.notes_source import StoreNotesSource
from routivus.server.files import (
    WorkspaceFileError,
    create_entry,
    delete_entry,
    list_entries,
    read_image,
    read_text_file,
    reference_hint,
    search_paths,
    write_text_file,
)
from routivus.server.insights import router_insights as build_router_insights
from routivus.server.logging_setup import install_token_redaction
from routivus.server.memory_view import memory_payload
from routivus.server.plan_review import PlanReviewBridge
from routivus.server.routing import (
    SessionRouter,
    prewarm_shared_assets,
    route_timeout_seconds,
    router_payload,
)
from routivus.server.terminal import (
    TerminalSession,
    TerminalSpec,
    TerminalUnavailableError,
    build_env,
    default_shell,
    new_terminal_id,
    select_backend,
)
from routivus.server.storage import EventRecord, MessageRecord, NoteConflictError, NoteRecord, SessionRecord, WorkspaceStore
from routivus.server.projects import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRegistryError,
    UnsafeProjectPathError,
)
from routivus.server.schemas import (
    HealthResponse,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectStats,
    ProjectUpdateRequest,
    ActivityResponse,
    MessageResponse,
    ModelLimitView,
    NoteCreateRequest,
    NoteResponse,
    NoteUpdateRequest,
    NoteStatsResponse,
    PinRequest,
    SessionCreateRequest,
    SessionResponse,
    SessionUpdateRequest,
    ActiveProviderRequest,
    ConfigSnapshot,
    DesktopInfoResponse,
    FileCreateBody,
    FileWriteBody,
    ModelRequest,
    ProviderCreateRequest,
    ProviderKeyRequest,
    ProviderUpdateRequest,
    ProviderView,
    RootGrantRequest,
    SkillDetail,
    SkillView,
    SkillWriteBody,
    SmartRouterRequest,
    TierRequest,
    TierView,
)

logger = logging.getLogger("routivus.server")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID", "").strip() or uuid4().hex
        request.state.request_id = request_id
        started = datetime.now().timestamp()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request failed request_id=%s path=%s", request_id, request.url.path)
            raise
        response.headers["X-Request-ID"] = request_id
        elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
        logger.info(
            "%s %s status=%s duration_ms=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """HTTP 侧访问令牌校验。

    此前 ``ROUTIVUS_SERVER_TOKEN`` **只**校验 WebSocket，REST 完全裸奔：
    本机任意进程都能注册项目、驱动 Agent 工具，还能顺着同一个 session socket
    自己批准自己的高危调用。桌面端用"每次启动的随机 token"把这条口子堵上。

    ``/healthz`` 与静态资源不校验：前者供桌面壳在拿到 token 之前轮询就绪，
    后者不含任何敏感数据。
    """

    def __init__(self, app: Any, token: str) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._token = token

    @staticmethod
    def extract_token(request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        return token or request.query_params.get("token", "")

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if not self._token or not request.url.path.startswith("/api/"):
            return await call_next(request)
        if self.extract_token(request) == self._token:
            return await call_next(request)
        return _error_response(request, 401, "not_authenticated", "缺少或错误的访问令牌")


def _desktop_mode() -> bool:
    raw = os.environ.get("ROUTIVUS_DESKTOP", "").strip().lower()
    return raw not in ("", "0", "off", "false", "no")


def _legacy_user_dir(config: ServerConfig) -> str | None:
    """旧数据目录（``~/.routivus``）仍存在且与新目录不同时返回它，供界面提示迁移。"""
    legacy = Path.home() / ".routivus"
    try:
        if not legacy.is_dir():
            return None
        if legacy.resolve() == Path(config.user_dir).expanduser().resolve():
            return None
    except OSError:  # pragma: no cover - 解析失败时不做提示
        return None
    return str(legacy)


def _build_config_manager(config: ServerConfig) -> Any:
    """构造面向「用户层」的配置管理器（供 Web Console 配置接口使用）。

    ``project_dir`` 指向一个不会存在的占位目录：Web Console 的 provider /
    档位配置属于用户级设置，没有"当前项目"上下文。若沿用默认的
    ``cwd/.routivus``，``provider_layer()`` 会把用户级配置误报成 project 层。
    """
    from routivus.config.manager import ConfigManager

    return ConfigManager(
        user_dir=config.user_dir,
        project_dir=Path(config.user_dir).expanduser() / ".no-project-context",
        load_env=False,
    )


def _build_skill_registry(
    manager: Any,
    root: Path,
    settings: Any,
    audit: Any,
    *,
    builtin_root: Path | None = None,
) -> Any | None:
    """构造只读 Skill 注册表；失败降级为 None（不能挡住会话创建）。

    配置以 `settings.skills_*` 为单一事实来源（`load_settings` 已合并 skills.json
    与 env），目录扫描用同一个 ConfigManager 的 user_dir，保证命令与配置页看到的
    enabled 覆盖一致。索引注入 system prompt，正文由 `load_skill` 工具按需加载。
    """
    try:
        from routivus.config.skills import SkillConfigManager
        from routivus.skill.models import SkillConfig
        from routivus.skill.registry import SkillRegistry

        skill_manager = SkillConfigManager(
            user_dir=getattr(manager, "user_dir", None),
            project_root=root,
            env=getattr(manager, "env", None),
        )
        config = SkillConfig(
            enabled=bool(getattr(settings, "skills_enabled", True)),
            max_index_items=int(getattr(settings, "skills_max_index_items", 20)),
            max_index_chars=int(getattr(settings, "skills_max_index_chars", 4096)),
            max_skill_chars=int(getattr(settings, "skills_max_chars", 32_000)),
            max_reference_chars=int(getattr(settings, "skills_max_reference_chars", 16_000)),
            max_loaded_chars=int(getattr(settings, "skills_max_loaded_chars", 64_000)),
        )
        return SkillRegistry(
            project_root=root,
            config=config,
            config_manager=skill_manager,
            builtin_root=builtin_root,
            audit=audit,
        )
    except Exception:  # noqa: BLE001 - Skill 目录损坏不能挡住会话创建
        logger.warning("Skill 注册表初始化失败，本次会话不启用 Skill", exc_info=True)
        return None


def _skill_view(info: Any) -> SkillView:
    """SkillInfo → 只读响应模型（不下发正文与参考资料）。"""
    return SkillView(
        name=str(getattr(info, "name", "")),
        description=str(getattr(info, "description", "") or ""),
        source=str(getattr(info, "source", "") or ""),
        version=getattr(info, "version", None),
        enabled=bool(getattr(info, "enabled", True)),
        valid=bool(getattr(info, "valid", True)),
        error=str(getattr(info, "error", "") or ""),
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or uuid4().hex


def _error_response(request: Request, status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def _to_response(record, store: WorkspaceStore | None = None) -> ProjectResponse:  # type: ignore[no-untyped-def]
    stats = store.project_stats(record.id) if store is not None else ProjectStats()
    return ProjectResponse(
        id=record.id,
        name=record.name,
        root_path=record.root_path,
        branch=record.branch,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        stats=ProjectStats(**stats) if isinstance(stats, dict) else stats,
    )


def _session_response(record: SessionRecord) -> SessionResponse:
    return SessionResponse.model_validate(record.__dict__)


def _message_response(record: MessageRecord) -> MessageResponse:
    return MessageResponse.model_validate(record.__dict__)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(value)))
    except (TypeError, ValueError):
        return default


def _preview(text: str, limit: int = 240) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _note_response(record: NoteRecord, registry: ProjectRegistry, *, detail: bool = True) -> NoteResponse:
    project_name = None
    if record.project_id:
        try:
            project_name = registry.get(record.project_id).name
        except ProjectNotFoundError:
            # A deleted project leaves its notes readable from the workspace
            # view, but its former name must not make the API fail.
            project_name = None
    return NoteResponse(
        id=record.id, project_id=record.project_id, project_name=project_name,
        title=record.title, body_markdown=record.body_markdown if detail else "",
        preview=_preview(record.body_markdown), tags=list(record.tags), pinned=record.pinned,
        created_at=record.created_at, updated_at=record.updated_at, version=record.version,
    )


def _approval_decision(message_type: str, payload: dict[str, Any]) -> ApprovalDecision:
    """把客户端的审批回执映射成 ApprovalDecision。

    reason 字符串沿用 TUI 已有的约定（`routivus/tui/controller.py:1014-1031`）。
    注意「本会话放行」(scope=session) 不在这里处理：它与「改参执行」可以同时
    出现，所以放行是 app 层的独立副作用（`policy.allow_all()`），不是决策本身。
    """
    if message_type == "reject":
        return ApprovalDecision(allow=False, reason="user_rejected")
    args = payload.get("args")
    if isinstance(args, dict) and args:
        return ApprovalDecision(allow=True, args=args, reason="user_modified")
    if _wants_session_scope(payload):
        return ApprovalDecision(allow=True, reason="user_approved_allow_all")
    return ApprovalDecision(allow=True, reason="user_approved")


def _wants_session_scope(payload: dict[str, Any]) -> bool:
    return str(payload.get("scope", "")).strip().lower() == "session"


# ---------- 计划 / 团队事件 → 会话事件 ----------
#
# 前端 `PlanPayload` / `TeamPayload`（frontend/src/api/types.ts）只声明了它真正
# 渲染的字段，所以这里显式挑字段而不是整包 `asdict`：既能与前端契约对齐，也避免
# 把 `agent_event` 这类内部结构、以及 ReviewResult 里的证据长文塞进每一帧。


def _scope_candidates(plan: Any, task: Any) -> list[str]:
    """失败任务的写入范围候选（方案 15 §4.6）。

    **只做保守的结构化提取**，不调 LLM、不从自由文本里正则抠路径——候选里出现一个
    不存在的路径，用户点了只会被服务端拒掉，比"少几个候选"更糟。来源两处：

    1. 该任务**已声明**的 read/write claims（它本来就是读这些才失败，扩成 write 是最
       小合理猜测）；
    2. 同计划里其它任务声明的 **write** 范围（参照同伴）。

    候选 ≠ 授权：勾选与否由用户决定，服务端还会再过一遍资源策略校验。
    """
    out: list[str] = []

    def add(pattern: Any) -> None:
        text = str(pattern or "").replace("\\", "/").strip()
        if text and text not in out:
            out.append(text)

    for claim in getattr(task, "resource_claims", None) or []:
        add(getattr(claim, "pattern", ""))
    for other in getattr(plan, "tasks", None) or []:
        if other is task:
            continue
        for claim in getattr(other, "resource_claims", None) or []:
            if str(getattr(claim, "access", "")) == "write":
                add(getattr(claim, "pattern", ""))
    return out[:8]


def _task_card_view(task: Any, *, mode: str, plan: Any = None) -> dict[str, Any]:
    """单个子任务的卡片视图（PlanTask 与 TeamTask 共用基础字段）。"""
    view: dict[str, Any] = {
        "id": str(getattr(task, "id", "")),
        "title": str(getattr(task, "title", "") or getattr(task, "description", "")),
        "description": str(getattr(task, "description", "")),
        "deps": [str(dep) for dep in getattr(task, "deps", None) or []],
        "status": str(getattr(task, "status", "pending")),
    }
    result = str(getattr(task, "result", "") or "")
    if result:
        view["result"] = result
    if mode == "team":
        view["owner_role"] = str(getattr(task, "owner_role", ""))
        criteria = [str(item) for item in getattr(task, "acceptance_criteria", None) or []]
        if criteria:
            view["acceptance_criteria"] = criteria
        # 断点续跑（方案 15 §4.5）：任务的资源声明要下发，前端才能渲染"允许修改范围"的
        # 候选项；失败分类下沉到任务级，卡片能逐任务解释原因。全部是可选字段——
        # 既有前端契约只增不改。
        claims = getattr(task, "resource_claims", None) or []
        if claims:
            view["resource_claims"] = [
                {
                    "pattern": str(getattr(claim, "pattern", "")),
                    "access": str(getattr(claim, "access", "read")),
                }
                for claim in claims
            ]
        category = str(getattr(task, "failure_category", "") or "")
        if category:
            view["failure_category"] = category
        if bool(getattr(task, "needs_scope", False)):
            view["needs_scope"] = True
            view["scope_candidates"] = _scope_candidates(plan, task)
    return view


def _plan_card_view(plan: Any, *, mode: str) -> dict[str, Any]:
    """整份计划（Plan 与 TeamPlan 共用 goal / tasks / batches 三元组）。"""
    return {
        "goal": str(getattr(plan, "goal", "")),
        "tasks": [
            _task_card_view(task, mode=mode, plan=plan)
            for task in getattr(plan, "tasks", None) or []
        ],
        "batches": [
            [str(task_id) for task_id in batch] for batch in getattr(plan, "batches", None) or []
        ],
    }


# 卡片类事件：结构化状态只在 events 表里，重连快照必须回放它们才能重建卡片。
# approval 两类也在其中：审批卡是时间线条目（requested 插卡、resolved 定格终态），
# 不回放的话刷新后审批记录就消失了——此前只回五类，审批只活在内存里，答完即无痕。
_REPLAY_EVENT_TYPES = (
    "plan.updated",
    "team.updated",
    "tool.started",
    "tool.completed",
    "command.executed",
    "approval.requested",
    "approval.resolved",
)

# router.updated 是状态类事件：回放里只保留最后一条（见 _replay_card_events）。
_ROUTER_EVENT_TYPE = "router.updated"

# Team 的可恢复快照（方案 15 §4.2）：任务图 + 各任务状态 + 续跑计数。**刻意不进**
# `_REPLAY_EVENT_TYPES`——那是给前端重建卡片的，快照是服务端内部状态（前端只需
# `team.updated`），全量回放它会白白撑大每帧载荷。读取时按类型取最近一条即可
# （`list_card_events` 的类型过滤正好给它一个独立窗口，不会被卡片事件挤掉）。
_TEAM_SNAPSHOT_EVENT = "team.snapshot"

# 卡片回放窗口：按类型过滤后取最近 N 条。高频事件已不再挤占窗口——message.delta
# 改为只广播、不落库（Optimization 01 §5.1），此前它每 token 一条会把卡片挤出窗口，
# 长会话重连就丢最新卡片（07 §4.6b）。
_REPLAY_CARD_LIMIT = 1000

# 思考段落库上限（字符）：超出截断并标注。只影响展示，不进 agent 上下文（07 §4.9）。
MAX_THINKING_SEGMENT_CHARS = 20_000
_THINKING_TRUNCATED_SUFFIX = "…（思考过长已截断）"


def _replay_card_events(events: list[EventRecord]) -> list[dict[str, Any]]:
    """把卡片类历史事件整理成快照回放列表。

    调用方现已用 `storage.list_card_events` 按类型取"最近 N 条"，并对每条补
    `occurred_at`：前端按时间戳把 messages 与 replay 归并成一条时间线
    （方案 07 §4.6a）。

    `router.updated` **全量保留**（不再只留最后一条）：每轮路由才 1 条事件，
    量可忽略；保留后"换档提示"刷新后仍在原位，也顺带支持档位历史（方案 08 §4.4）。
    前端对该事件是纯状态更新（后到覆盖），全量回放无副作用。
    """
    replay: list[dict[str, Any]] = []
    for item in events:
        if item.event_type in _REPLAY_EVENT_TYPES or item.event_type == _ROUTER_EVENT_TYPE:
            replay.append({
                "type": item.event_type,
                "sequence": item.sequence,
                "occurred_at": item.occurred_at,
                "data": item.data,
            })
    return replay


def _task_card_payload(item: Any, *, mode: str) -> dict[str, Any]:
    """把一个 PlanEvent / TeamEvent 摊平成前端直接消费的卡片载荷。"""
    data: dict[str, Any] = {"kind": str(getattr(item, "kind", ""))}
    message = str(getattr(item, "message", "") or "")
    if message:
        data["message"] = message
    plan = getattr(item, "plan", None)
    if plan is not None:
        data["plan"] = _plan_card_view(plan, mode=mode)
    batch = getattr(item, "batch", None)
    if batch:
        data["batch"] = [str(task_id) for task_id in batch]
    task = getattr(item, "task", None)
    if task is not None:
        data["task"] = _task_card_view(task, mode=mode, plan=plan)
    if mode == "team":
        data["team_id"] = str(getattr(item, "team_id", "") or "")
        agent_id = str(getattr(item, "agent_id", "") or "")
        if agent_id:
            data["agent_id"] = agent_id
        role = str(getattr(item, "role", "") or "")
        if role:
            data["role"] = role
        attempt = int(getattr(item, "attempt", 0) or 0)
        if attempt:
            data["attempt"] = attempt
        # 失败原因分类（如 repair_scope_missing / task_failed）：前端据此解释失败原因。
        category = str(getattr(item, "failure_category", "") or "")
        if category:
            data["failure_category"] = category
        # 断点续跑（方案 15 §4.4）：收尾事件带上"还能不能续、已续过几次"，
        # 前端据此决定团队卡上要不要给「继续」按钮。
        if bool(getattr(item, "resumable", False)):
            data["resumable"] = True
        count = int(getattr(item, "resume_count", 0) or 0)
        if count:
            data["resume_count"] = count
    return data


def _parse_scope_claims(raw: Any) -> tuple[list[Any], str]:
    """解析 `team_resume` 的 `scope`（方案 15 §4.7）→ (claims, error)。

    接受 `["a/**", …]` 与 `[{pattern, access}, …]` 两种形态（后者便于前端带上
    access）。**结构非法直接回错误**而不是当成空范围——空范围会被当成"这不是权限类
    续跑"，把用户的意图悄悄换成另一种行为。
    """
    from routivus.agent.team import ResourceClaim

    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return [], "scope 必须是数组"
    claims: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            pattern, access = item.strip(), "write"
        elif isinstance(item, dict):
            pattern = str(item.get("pattern", "")).strip()
            access = (str(item.get("access", "write")).strip().lower() or "write")
        else:
            return [], "scope 的每一项必须是字符串或 {pattern, access} 对象"
        if not pattern:
            continue
        if access not in {"read", "write"}:
            return [], f"未知的 access：{access}"
        claims.append(ResourceClaim(pattern, access))
    return claims, ""


def _first_needs_scope_task(plan: Any) -> str:
    """第一个"补个范围就能救"的失败任务（`needs_scope` 由执行器标记）。"""
    for task in getattr(plan, "tasks", None) or []:
        if bool(getattr(task, "needs_scope", False)) and str(getattr(task, "status", "")) == "failed":
            return str(getattr(task, "id", "") or "")
    return ""


def _parse_task_command(content: str) -> tuple[str, str, dict[str, Any]]:
    """把用户输入解析为 (turn_kind, goal, options)。

    前缀约定与 TUI 保持一致（`routivus/tui/controller.py:358-367`）：
    `/plan <任务>`、`/team <任务>`，其余一律普通对话。
    返回的 turn_kind ∈ {"chat", "plan", "team", "team_resume"}；`team_resume` 只用于
    把已移除的 `/team resume` 拦下来回一条明确错误，不是可执行回合。
    """
    text = content.strip()
    lowered = text.lower()
    if lowered.startswith("/plan"):
        return "plan", text[len("/plan") :].strip(), {}
    if lowered.startswith("/team"):
        rest = text[len("/team") :].strip()
        if rest.lower() == "resume" or rest.lower().startswith("resume "):
            return "team_resume", rest, {}
        return "team", rest, {}
    return "chat", text, {}


def _event_payload(event: EventRecord) -> dict[str, Any]:
    return {
        "type": event.event_type,
        "event_id": event.event_id,
        "sequence": event.sequence,
        "session_id": event.session_id,
        "project_id": event.project_id,
        "occurred_at": event.occurred_at,
        "data": event.data,
    }


# 只广播、不落库的事件类型（plans/Optimization/01-runtime-resource-footprint.md §5.1）。
# 判定标准是"落库之后无人读"：它们都不在 `_REPLAY_EVENT_TYPES` 里（重连回放用不上），
# 正文也已由 `message.segment` 落到 messages 表，前端只在 WS 实时消费。
# `message.delta` 每 token 一条，占事件表条数的 73%，是磁盘 IOPS 的主因。
_EPHEMERAL_EVENT_TYPES = frozenset({"message.delta"})


def _ephemeral_payload(
    event_type: str, session: SessionRecord, data: dict[str, Any] | None
) -> dict[str, Any]:
    """瞬时事件的载荷：字段与 `_event_payload` 对齐，但没有 event_id / sequence。

    `sequence` 为 None 是刻意的：前端 `ws/sessionSocket.ts` 只在 sequence 是 number 时
    参与去重与 lastSequence 推进，缺失即直接透传——正是"不占序号"想要的语义。该处是
    **严格递增**检测而非连续性校验，所以序号出现空洞也不影响后续事件。
    """
    return {
        "type": event_type,
        "event_id": "",
        "sequence": None,
        "session_id": session.id,
        "project_id": session.project_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "data": data or {},
    }


def _build_default_agent(
    project: Any,
    session: SessionRecord,
    *,
    notes_source: Any | None = None,
) -> Any:
    """Build the existing ReAct stack for a project-scoped WebSocket turn.

    `notes_source` 由 `create_app` 用 `functools.partial` 绑定（见
    plans/tools/notes-read-tool.md §3.3）：传进来才会注册 notes_list / notes_read，
    否则工具名根本不出现 —— 测试与嵌入方自带的 agent 工厂因此不受影响。
    """
    from routivus.agent.react import ReActAgent
    from routivus.config.manager import ConfigManager
    from routivus.config.settings import load_settings
    from routivus.llm.factory import create_client
    from routivus.memory.manager import MemoryManager
    from routivus.safety.audit import AuditLogger
    from routivus.safety.guards import guard_tool_call
    from routivus.safety.hitl import HITLPolicy
    from routivus.tool.builtin import build_registry

    root = Path(project.root_path).resolve()
    manager = ConfigManager(project_dir=root / ".routivus")
    settings = load_settings(manager)
    if settings.provider_missing or not settings.api_base or not settings.api_key:
        raise RuntimeError("Provider 未配置，无法启动 Agent")
    llm = create_client(
        api_base=settings.api_base,
        api_key=settings.api_key,
        model=settings.model,
        max_tokens=settings.max_output_tokens,
        max_tokens_field=settings.max_tokens_field,
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    audit = AuditLogger(root / ".routivus" / "audit.log", session_id=session.id)
    # Skill 注册表：只读扫描 builtin/user/project 三处目录，索引注入 system prompt，
    # 正文由 load_skill 工具按需加载（见 plans/enhancement/02-skill-integration.md）。
    skills = _build_skill_registry(manager, root, settings, audit)
    tools = build_registry(
        base_dir=root,
        max_output_chars=settings.max_tool_output_chars,
        guard=lambda name, args: guard_tool_call(root, name, args),
        audit=audit,
        ask_user_enabled=settings.ask_user_enabled,
        skill_registry=skills,
        # 笔记工具：project_id 在这里捕获、不进工具参数，模型无法指定别的项目。
        # store 先过一层适配器：把 NoteConflictError 翻译成工具层的异常词汇
        # （tool/ 是下层，不 import server/，见 plans/tools/notes-write-tools.md §3.2）。
        notes_source=StoreNotesSource(notes_source) if notes_source is not None else None,
        project_id=project.id,
        notes_write_enabled=settings.notes_write_enabled,
    )
    memory = MemoryManager(
        root,
        project_memory_max_chars=settings.project_memory_max_chars,
        memory_prompt_max_chars=settings.memory_prompt_max_chars,
    )
    agent = ReActAgent(
        llm=llm,
        tools=tools,
        settings=settings,
        approval_policy=HITLPolicy(enabled=settings.hitl),
        audit=audit,
        memory_manager=memory,
        skill_registry=skills,
    )
    # SmartRouter 需要同一个 ConfigManager 才能读到四档配置、解析目标 provider 的
    # API Key 并换 `agent.llm`（与 TUI 的 SessionController 持有 manager 同构）。
    agent.config_manager = manager
    return agent


def _record_payload(record: Any) -> dict[str, Any]:
    return jsonable_encoder(record.model_dump(mode="json") if hasattr(record, "model_dump") else record)


@asynccontextmanager
async def _terminal_lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """服务退出时收割所有终端。

    没有这一步，Ctrl-C 停服务会留下孤儿的 cmd.exe 及其子进程 —— 正是 Phase 5
    完成标准后半句要求覆盖的场景。
    """
    # 智能路由已开启时，启动即在**当前线程同步**预热重资产。此刻尚未 accept
    # 连接，阻塞事件循环是安全的；关键是首次重型 import 必须远离后台线程 ——
    # 否则会与事件循环首次创建 AnyIO worker 线程竞态死锁（窗口打不开）。
    # 桌面端已在 `__main__` 的事件循环之前预热过，这里通常已是空操作。
    if getattr(app.state, "smart_router_startup_enabled", False):
        prewarm_shared_assets(blocking=True)
    yield
    for terminal in list(getattr(app.state, "terminals", {}).values()):
        try:
            await terminal.close("server_shutdown")
        except Exception:  # pragma: no cover - 退出路径尽力而为
            logger.debug("terminal shutdown failed terminal_id=%s", terminal.spec.terminal_id, exc_info=True)
    # 共享长连接随进程收尾。不显式关也不影响功能（OS 会回收），但关掉能让 -wal/-shm
    # 干净落盘，也避免个别平台在进程退出时留下句柄告警（Optimization 01 §5.2）。
    store = getattr(app.state, "workspace_store", None)
    if store is not None:
        try:
            store.close()
        except Exception:  # pragma: no cover - 退出路径尽力而为
            logger.debug("workspace store close failed", exc_info=True)


def create_app(
    *,
    registry: ProjectRegistry | None = None,
    config: ServerConfig | None = None,
    store: WorkspaceStore | None = None,
    agent_factory: Callable[..., Any] | None = None,
    terminal_factory: Callable[[TerminalSpec], Any] | None = None,
) -> FastAPI:
    """Create an isolated application instance for production or tests."""

    resolved_config = config or ServerConfig.from_env()
    # WebSocket 的令牌只能走查询参数，uvicorn 会把整条 URL 打进日志 → 挂脱敏。
    install_token_redaction()
    project_registry = registry or ProjectRegistry(
        resolved_config.projects_file,
        resolved_config.workspace_roots,
    )
    if store is not None:
        workspace_store = store
    else:
        database_path = resolved_config.database_path
        # An injected registry is commonly used by tests/embedders. Keep its
        # default data isolated beside the registry instead of touching the
        # user's global directory.
        if config is None and registry is not None:
            database_path = registry.storage_path.with_name("workspace.sqlite3")
        try:
            workspace_store = WorkspaceStore(database_path)
        except (PermissionError, sqlite3.OperationalError):
            if config is not None:
                raise
            # A read-only user profile should not prevent an embedded server
            # from starting. This is only a fallback; configured paths are
            # used whenever they are writable.
            fallback = Path(tempfile.gettempdir()) / "routivus-workspace.sqlite3"
            logger.warning("database path is not writable; using %s", fallback)
            workspace_store = WorkspaceStore(fallback)
    app = FastAPI(title="Routivus Web Console", version=__version__, lifespan=_terminal_lifespan)
    app.state.project_registry = project_registry
    app.state.server_config = resolved_config
    app.state.workspace_store = workspace_store
    # 默认工厂用 partial 绑定笔记数据源：调用点仍是 factory(project, session)
    # （ensure_session_agent），测试里注入的 lambda project, session 一律不受影响。
    app.state.agent_factory = agent_factory or functools.partial(
        _build_default_agent, notes_source=workspace_store
    )
    app.state.ws_connections = {}
    app.state.session_agents = {}
    app.state.running_tasks = {}
    app.state.session_approvals = {}
    # 计划 / 团队审阅桥接（计划模式的人工审阅往返）。
    app.state.session_reviews = {}
    # 会话级 SmartRouter 运行态（惰性创建；None 表示该会话已确认不可用）
    app.state.session_routers = {}
    # 项目级长期记忆库句柄（按项目根缓存，惰性打开；见 project_memory）
    app.state.project_memories = {}
    app.state.terminals = {}
    app.state.terminal_factory = terminal_factory or (
        lambda spec: select_backend(
            spec,
            resolved_config.terminal_backend,
            command_timeout=resolved_config.terminal_command_timeout,
        )
    )
    # 先注册 auth、后注册 RequestContext：Starlette 的 add_middleware 是「前插」，
    # 所以后注册的在外层。这样被令牌拒绝时 request_id 已经写入 request.state，
    # 401 响应也能带上可追踪的 X-Request-ID。
    if resolved_config.ws_auth_token:
        app.add_middleware(TokenAuthMiddleware, token=resolved_config.ws_auth_token)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(resolved_config.allowed_hosts) or ["localhost"],
    )
    if resolved_config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_config.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-Request-ID"],
        )

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError):
        return _error_response(request, 422, "invalid_value", str(exc))

    @app.exception_handler(KeyError)
    async def handle_key_error(request: Request, exc: KeyError):
        return _error_response(request, 404, "resource_not_found", str(exc).strip("'"))

    @app.exception_handler(NoteConflictError)
    async def handle_note_conflict(request: Request, exc: NoteConflictError):
        return _error_response(request, 409, "note_conflict", str(exc))

    @app.exception_handler(WorkspaceFileError)
    async def handle_workspace_file_error(request: Request, exc: WorkspaceFileError):
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(ProjectRegistryError)
    async def handle_registry_error(request: Request, exc: ProjectRegistryError):
        if isinstance(exc, ProjectNotFoundError):
            return _error_response(request, 404, "project_not_found", str(exc))
        if isinstance(exc, ProjectAlreadyExistsError):
            return _error_response(request, 409, "project_already_exists", str(exc))
        if isinstance(exc, UnsafeProjectPathError):
            return _error_response(request, 422, "unsafe_project_path", str(exc))
        return _error_response(request, 500, "project_registry_error", str(exc))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg", "输入无效")}
            for item in exc.errors()
        ]
        return _error_response(request, 422, "validation_error", "请求参数无效", details)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        return _error_response(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.exception("unhandled server error request_id=%s", _request_id(request), exc_info=exc)
        return _error_response(request, 500, "internal_error", "服务器内部错误")

    router = APIRouter(prefix="/api")

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="routivus-server",
            version=__version__,
            request_id=_request_id(request),
        )

    @router.get("/projects", response_model=list[ProjectResponse])
    async def list_projects() -> list[ProjectResponse]:
        return [_to_response(record, workspace_store) for record in project_registry.list()]

    @router.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(payload: ProjectCreateRequest) -> ProjectResponse:
        return _to_response(project_registry.create(payload.name, payload.root_path), workspace_store)

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    async def get_project(project_id: str) -> ProjectResponse:
        return _to_response(project_registry.get(project_id), workspace_store)

    @router.patch("/projects/{project_id}", response_model=ProjectResponse)
    async def update_project(project_id: str, payload: ProjectUpdateRequest) -> ProjectResponse:
        return _to_response(
            project_registry.update(
                project_id,
                name=payload.name,
                root_path=payload.root_path,
            ), workspace_store
        )

    @router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_project(project_id: str) -> Response:
        """移除项目注册：只摘 projects.json，不删除磁盘文件与会话 / 笔记数据。

        运行中的轮次还在往该项目写事件与审计，先拦住更安全。判定以**内存任务表**
        为准——数据库里的 running 可能是服务重启后留下的陈旧状态。
        """
        require_project(project_id)
        busy = [
            item.id
            for item in workspace_store.list_sessions(project_id, limit=500)
            if item.id in running_tasks and not running_tasks[item.id].done()
        ]
        if busy:
            raise ApiError(409, "project_busy", "该项目仍有运行中的会话，请先停止后再移除")
        project_registry.delete(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def require_project(project_id: str):  # type: ignore[no-untyped-def]
        return project_registry.get(project_id)

    def require_session(session_id: str, project_id: str | None = None) -> SessionRecord:
        session = workspace_store.get_session(session_id)
        if session is None or (project_id is not None and session.project_id != project_id):
            raise ApiError(404, "session_not_found", "会话不存在")
        return session

    def page_offset(offset: int, cursor: str | None) -> int:
        if cursor is None or not cursor.strip():
            return max(0, offset)
        try:
            return max(0, int(cursor))
        except ValueError:
            raise ApiError(422, "invalid_cursor", "分页游标无效")

    @router.get("/activity", response_model=list[ActivityResponse])
    async def activity(from_date: str | None = Query(default=None, alias="from"), to_date: str | None = Query(default=None, alias="to")) -> list[ActivityResponse]:
        """按**本地日**聚合的活动量（热力图）。日期格式非法回 422，而不是悄悄返回空。"""
        try:
            records = workspace_store.activity(from_date, to_date)
        except ValueError as exc:
            raise ApiError(422, "invalid_date", str(exc)) from exc
        return [ActivityResponse(**item) for item in records]

    @router.get("/usage/summary")
    async def usage_summary(days: int = 30) -> dict[str, Any]:
        """首页 token 面板（方案 12）：今日 / 区间 / 累计 + 按天 + 按项目 + 按档位。

        `days` 越界由存储层钳到 7–90（前端传错不该让首页空掉）。返回体是纯数据视图，
        项目名在这里补上——注册表在 app 层，存储层只认 project_id。
        """
        payload = workspace_store.usage_summary(days=days)
        names = {record.id: record.name for record in project_registry.list()}
        for item in payload["by_project"]:
            item["name"] = names.get(str(item.get("project_id", "")), "")
        return payload

    @router.get("/router/insights")
    async def router_insights(days: int = 30) -> dict[str, Any]:
        """SmartRouter 数据看板（方案 13）：只读聚合，实现见 server/insights.py。

        读文件、探测依赖、按需解析产物 metadata 都是同步活儿，丢到工作线程——
        与智能路由同一条理由：别让事件循环被磁盘与 import 阻塞。
        """
        return await asyncio.to_thread(build_router_insights, workspace_store, days=days)

    @router.get("/projects/{project_id}/sessions", response_model=list[SessionResponse])
    async def list_sessions(project_id: str, limit: int = 50, offset: int = 0, cursor: str | None = None) -> list[SessionResponse]:
        require_project(project_id)
        return [_session_response(item) for item in workspace_store.list_sessions(project_id, limit, page_offset(offset, cursor))]

    @router.get("/projects/{project_id}/sessions/current", response_model=SessionResponse | None)
    async def current_session(project_id: str) -> SessionResponse | None:
        require_project(project_id)
        session = workspace_store.latest_session(project_id)
        return _session_response(session) if session else None

    @router.post("/projects/{project_id}/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
    async def create_session(project_id: str, payload: SessionCreateRequest) -> SessionResponse:
        require_project(project_id)
        return _session_response(workspace_store.create_session(project_id, payload.title))

    @router.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str) -> SessionResponse:
        return _session_response(require_session(session_id))

    @router.get("/projects/{project_id}/sessions/{session_id}", response_model=SessionResponse)
    async def get_project_session(project_id: str, session_id: str) -> SessionResponse:
        return _session_response(require_session(session_id, project_id))

    @router.patch("/sessions/{session_id}", response_model=SessionResponse)
    async def update_session(session_id: str, payload: SessionUpdateRequest) -> SessionResponse:
        require_session(session_id)
        updated = workspace_store.update_session(session_id, title=payload.title)
        assert updated is not None
        return _session_response(updated)

    @router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_session(session_id: str) -> Response:
        """删除会话（连同它的消息与事件，级联在 storage 层）。

        运行中的会话拒绝删除，判定以**内存任务表**为准（与「移除项目」一致：库里
        的 running 可能是服务重启后留下的陈旧状态）。不拦的话有两个后果：那一轮还会
        继续往已删会话写消息与事件（外键约束会让它抛 KeyError），以及挂起的审批
        Future 永远没人应答。审批只有在一轮运行中才可能存在，所以 busy 判定就是它
        的保护，这里不需要再去 cancel_pending。

        删除后顺手丢掉这个会话的进程内状态：agent / 审批桥 / 计划审阅 / 可续跑执行器
        / 路由器。不清的话这些字典会随着删会话一直涨，而且被删会话的 agent 还可能被
        后续连接复用。
        """
        require_session(session_id)
        task = running_tasks.get(session_id)
        if task is not None and not task.done():
            raise ApiError(409, "session_busy", "会话正在运行，请先停止后再删除")
        if not workspace_store.delete_session(session_id):
            raise ApiError(404, "session_not_found", "会话不存在")
        session_agents.pop(session_id, None)
        session_approvals.pop(session_id, None)
        session_reviews.pop(session_id, None)
        session_routers.pop(session_id, None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
    async def list_messages(session_id: str, before: str | None = None, limit: int = 100, offset: int = 0) -> list[MessageResponse]:
        require_session(session_id)
        records = workspace_store.list_messages(session_id, max(1, min(limit, 500)), 0)
        if before:
            if before.isdigit():
                records = records[: max(0, int(before))]
            else:
                index = next((i for i, item in enumerate(records) if item.id == before), len(records))
                records = records[:index]
            records = records[max(0, len(records) - max(1, min(limit, 500))):]
        else:
            records = records[max(0, offset): max(0, offset) + max(1, min(limit, 500))]
        return [_message_response(item) for item in records]

    @router.get("/sessions/{session_id}/events")
    async def list_session_events(session_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        require_session(session_id)
        return [_event_payload(item) for item in workspace_store.list_events(session_id, after, limit)]

    @router.get("/completions")
    async def completions(
        q: str = "",
        cursor: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Composer 的 slash 命令补全候选（只读、无副作用、永不 404）。

        提示 Web 端真正会执行的命令（白名单见 completions.py）；传入 session_id
        时从会话 agent 的 ConfigManager / memory_manager 生成动态值候选
        （/model 的模型名、/memory 的记忆 ID）。会话缺失时优雅降级为静态候选。
        """
        session_agent = session_agents.get(session_id) if session_id else None
        return completion_payload(
            q,
            cursor,
            manager=getattr(session_agent, "config_manager", None),
            agent=session_agent,
        )

    @router.get("/sessions/{session_id}/memory")
    async def get_session_memory(session_id: str, limit: int = 20) -> dict[str, Any]:
        """会话所属项目的长期记忆条目（只读、无副作用）。

        侧栏 Memory 页签的数据源：命令改了记忆（/save、/memory delete）后由前端
        重新拉取。记忆是项目级数据，同一项目的不同会话看到同一份。库不存在或损坏
        时返回 `status="unavailable"` 的空视图，不报 500。
        """
        session = require_session(session_id)
        project = require_project(session.project_id)
        return memory_snapshot(session, project, limit=limit)

    async def emit(event_type: str, session: SessionRecord, data: dict[str, Any] | None = None) -> dict[str, Any]:
        event = workspace_store.append_event(session.id, session.project_id, event_type, data)
        return _event_payload(event)

    @router.post("/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, Any]:
        session = require_session(session_id)
        bridge = session_approvals.get(session_id)
        if bridge is not None:
            # 先把挂起的审批按拒绝落地，否则那个 Future 永远悬着。
            bridge.cancel_pending()
        task = running_tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        updated = workspace_store.update_session(session_id, status="cancelled")
        assert updated is not None
        event = await emit("session.status", updated, {"status": "cancelled"})
        return {"session": _session_response(updated).model_dump(mode="json"), "cancelled": True, "event": event}

    @router.get("/notes", response_model=list[NoteResponse])
    async def list_notes(scope: str = "global", query: str = "", limit: int = 50, offset: int = 0, cursor: str | None = None) -> list[NoteResponse]:
        if scope not in {"global", "all"}:
            raise ApiError(422, "invalid_note_scope", "全局入口的 scope 必须为 global")
        return [_note_response(item, project_registry, detail=True) for item in workspace_store.list_notes(scope="global", query=query, limit=limit, offset=page_offset(offset, cursor))]

    @router.get("/notes/stats", response_model=NoteStatsResponse)
    async def note_stats() -> NoteStatsResponse:
        return NoteStatsResponse(**workspace_store.note_stats())

    @router.post("/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
    async def create_note(payload: NoteCreateRequest) -> NoteResponse:
        if payload.project_id is not None:
            require_project(payload.project_id)
        record = workspace_store.create_note(payload.title, payload.body_markdown, payload.tags, payload.project_id)
        return _note_response(record, project_registry)

    @router.get("/notes/{note_id}", response_model=NoteResponse)
    async def get_note(note_id: str) -> NoteResponse:
        record = workspace_store.get_note(note_id)
        if record is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return _note_response(record, project_registry)

    @router.patch("/notes/{note_id}", response_model=NoteResponse)
    async def update_note(note_id: str, payload: NoteUpdateRequest) -> NoteResponse:
        if workspace_store.get_note(note_id) is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        if "project_id" in payload.model_fields_set and payload.project_id is not None:
            require_project(payload.project_id)
        kwargs = payload.model_dump(exclude_unset=True)
        has_project_id = "project_id" in kwargs
        project_id = kwargs.pop("project_id", None)
        version = kwargs.pop("version", None)
        if has_project_id:
            updated = workspace_store.update_note(note_id, **kwargs, project_id=project_id, expected_version=version)
        else:
            updated = workspace_store.update_note(note_id, **kwargs, expected_version=version)
        if updated is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return _note_response(updated, project_registry)

    @router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_note(note_id: str) -> Response:
        if not workspace_store.delete_note(note_id):
            raise ApiError(404, "note_not_found", "笔记不存在")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/notes/{note_id}/pin", response_model=NoteResponse)
    async def pin_note(note_id: str, payload: PinRequest | None = None) -> NoteResponse:
        updated = workspace_store.set_note_pinned(note_id, payload.pinned if payload else None)
        if updated is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return _note_response(updated, project_registry)

    @router.get("/projects/{project_id}/notes", response_model=list[NoteResponse])
    async def list_project_notes(project_id: str, query: str = "", limit: int = 50, offset: int = 0, cursor: str | None = None) -> list[NoteResponse]:
        require_project(project_id)
        return [_note_response(item, project_registry, detail=True) for item in workspace_store.list_notes(scope="project", project_id=project_id, query=query, limit=limit, offset=page_offset(offset, cursor))]

    @router.get("/projects/{project_id}/notes/{note_id}", response_model=NoteResponse)
    async def get_project_note(project_id: str, note_id: str) -> NoteResponse:
        record = require_project_note(project_id, note_id)
        return _note_response(record, project_registry)

    def require_project_note(project_id: str, note_id: str) -> NoteRecord:
        require_project(project_id)
        record = workspace_store.get_note(note_id)
        if record is None or record.project_id != project_id:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return record

    @router.patch("/projects/{project_id}/notes/{note_id}", response_model=NoteResponse)
    async def update_project_note(project_id: str, note_id: str, payload: NoteUpdateRequest) -> NoteResponse:
        record = require_project_note(project_id, note_id)
        if "project_id" in payload.model_fields_set and payload.project_id != project_id:
            raise ApiError(422, "invalid_note_scope", "项目范围接口不能改变笔记归属")
        kwargs = payload.model_dump(exclude_unset=True)
        kwargs.pop("project_id", None)
        version = kwargs.pop("version", None)
        updated = workspace_store.update_note(note_id, **kwargs, expected_version=version)
        if updated is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return _note_response(updated, project_registry)

    @router.delete("/projects/{project_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_project_note(project_id: str, note_id: str) -> Response:
        require_project_note(project_id, note_id)
        if not workspace_store.delete_note(note_id):
            raise ApiError(404, "note_not_found", "笔记不存在")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/projects/{project_id}/notes/{note_id}/pin", response_model=NoteResponse)
    async def pin_project_note(project_id: str, note_id: str, payload: PinRequest | None = None) -> NoteResponse:
        require_project_note(project_id, note_id)
        updated = workspace_store.set_note_pinned(note_id, payload.pinned if payload else None)
        if updated is None:
            raise ApiError(404, "note_not_found", "笔记不存在")
        return _note_response(updated, project_registry)

    @router.post("/projects/{project_id}/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
    async def create_project_note(project_id: str, payload: NoteCreateRequest) -> NoteResponse:
        require_project(project_id)
        record = workspace_store.create_note(payload.title, payload.body_markdown, payload.tags, project_id)
        return _note_response(record, project_registry)

    # ---------- 项目工作区文件 ----------
    # 只读列举 + 受控写入。路径校验、忽略规则与护栏都在 server/files.py 里，
    # 设计与取舍见 plans/enhancement/04-workspace-files.md。

    @router.get("/projects/{project_id}/files")
    async def list_project_files(
        project_id: str, path: str = "", include_ignored: bool = False
    ) -> dict[str, Any]:
        """列**一层**目录（逐层懒加载，不做递归扫描）。"""
        project = require_project(project_id)
        return list_entries(
            Path(project.root_path), path, include_ignored=include_ignored
        )

    @router.get("/projects/{project_id}/files/search")
    async def search_project_files(
        project_id: str, q: str = "", limit: int = 20
    ) -> dict[str, Any]:
        """按路径片段搜项目内文件（方案 05 §5.1）。

        只读、只匹配路径（不读文件内容），返回结构与文件树一致。与 `/completions`
        **不合并**：那是 slash 命令补全，语义与数据源都不同（方案 §6）。
        """
        project = require_project(project_id)
        return search_paths(Path(project.root_path), q, limit=limit)

    @router.get("/projects/{project_id}/file")
    async def read_project_file(project_id: str, path: str) -> dict[str, Any]:
        """读文本文件：二进制只给元信息；超限截断、解码失败标记 lossy（两者禁止保存）。"""
        project = require_project(project_id)
        return read_text_file(Path(project.root_path), path)

    @router.get("/projects/{project_id}/file/raw")
    async def read_project_file_raw(project_id: str, path: str) -> Response:
        """图片原始字节（给 `<img>` 用）。后缀 + magic bytes 双重校验。"""
        project = require_project(project_id)
        data, media_type = read_image(Path(project.root_path), path)
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @router.put("/projects/{project_id}/file")
    async def write_project_file(
        project_id: str, payload: FileWriteBody
    ) -> dict[str, Any]:
        """保存文件。人工保存不走 HITL（用户即批准者），但校验与审计一步不少。"""
        project = require_project(project_id)
        return write_text_file(
            Path(project.root_path),
            payload.path,
            payload.content,
            expected_version=payload.expected_version,
            force=payload.force,
        )

    @router.post(
        "/projects/{project_id}/files", status_code=status.HTTP_201_CREATED
    )
    async def create_project_entry(
        project_id: str, payload: FileCreateBody
    ) -> dict[str, Any]:
        """新建文件或目录（父目录必须已存在）。"""
        project = require_project(project_id)
        return create_entry(Path(project.root_path), payload.path, payload.kind)

    @router.delete("/projects/{project_id}/file")
    async def delete_project_file(
        project_id: str, path: str, recursive: bool = False
    ) -> dict[str, Any]:
        """删除项目内的文件或目录（不可逆，落审计）。

        `recursive` 必须显式声明：目录非空时不给它就报 409，避免"右键点错就把整棵树
        删掉"。路径越界、忽略目录（含 .git）与项目数据目录（.routivus）都会被拒，
        见 `files.delete_entry`。
        """
        project = require_project(project_id)
        return delete_entry(Path(project.root_path), path, recursive=recursive)

    # ---------- Web Console 配置接口 ----------
    # 桌面端与 Web 端共用。没有这组接口，用户就只能靠 REPL / CLI 配 provider，
    # 而本仓库两者都没有 —— 桌面版首次打开会直接不可用。

    from routivus.config.provider_service import (
        ProviderConfigService,
        validate_model,
        validate_name,
    )
    from routivus.config.smart_router_service import SmartRouterConfigService

    config_manager = _build_config_manager(resolved_config)
    provider_service = ProviderConfigService(config_manager, None, language="zh")
    tier_service = SmartRouterConfigService(config_manager, None, language="zh")

    # 开关开着才预热：供 lifespan 在启动时读取（关闭时不加载任何重资产）。
    app.state.smart_router_startup_enabled = bool(
        config_manager.smart_router_config().get("enabled", False)
    )

    def _provider_views() -> list[ProviderView]:
        views: list[ProviderView] = []
        for row in provider_service.list():
            name = str(row["name"])
            detail = provider_service.get(name) or {}
            views.append(
                ProviderView(
                    name=name,
                    display_name=row.get("display_name"),
                    api_base=str(row.get("api_base", "")),
                    default_model=str(row.get("default_model", "")),
                    models=list(row.get("models") or []),
                    has_key=bool(row.get("has_key")),
                    api_key_masked=str(detail.get("api_key_masked", "")),
                    is_base=bool(row.get("is_base")),
                    layer=str(row.get("layer") or "user"),
                    context_window=int(row.get("context_window") or 0),
                    model_limits={
                        str(model_name): ModelLimitView(**(limits or {}))
                        for model_name, limits in (row.get("model_limits") or {}).items()
                    },
                    max_output_tokens=int(row.get("max_output_tokens") or 0),
                    max_tokens_field=str(
                        row.get("max_tokens_field") or DEFAULT_MAX_TOKENS_FIELD
                    ),
                )
            )
        return views

    def _provider_view(name: str) -> ProviderView:
        for view in _provider_views():
            if view.name == name:
                return view
        raise ApiError(404, "provider_not_found", "Provider 不存在")

    def _snapshot() -> ConfigSnapshot:
        active_provider = ""
        active_model = ""
        context_window = 128_000
        max_output_tokens = 0
        max_tokens_field = DEFAULT_MAX_TOKENS_FIELD
        try:
            active = config_manager.active()
            active_provider = active.provider_name
            active_model = active.model
            context_window = active.context_window
            max_output_tokens = active.max_output_tokens
            max_tokens_field = active.max_tokens_field
        except Exception:
            # 尚未配置任何 provider 是正常的初始状态，不是错误
            pass
        smart = tier_service.get()
        return ConfigSnapshot(
            active_provider=active_provider,
            active_model=active_model,
            providers=_provider_views(),
            tiers=[TierView(**row) for row in tier_service.list_tiers()],
            smart_router_enabled=bool(smart.get("enabled")),
            user_dir=str(resolved_config.user_dir),
            legacy_user_dir=_legacy_user_dir(resolved_config),
            desktop=_desktop_mode(),
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            max_tokens_field=max_tokens_field,
        )

    def _ensure_ok(result: Any) -> None:
        if not getattr(result, "ok", False):
            raise ApiError(422, "config_rejected", str(getattr(result, "message", "配置被拒绝")))

    def _sync_smart_router_runtime(enabled: bool) -> None:
        """把开关同步到已缓存的会话 agent。

        agent.settings 是创建时的快照、不会自动刷新，若只改配置而不回写，已存在
        的会话会一直用旧开关 —— 表现就是「开了智能路由，旧对话仍不路由」。
        """
        for cached in app.state.session_agents.values():
            settings = getattr(cached, "settings", None)
            if settings is None or not hasattr(settings, "smart_router_enabled"):
                continue
            settings.smart_router_enabled = enabled
            settings.smart_router_saved = (
                (getattr(settings, "provider", ""), getattr(settings, "model", ""))
                if enabled
                else None
            )

    def _apply_smart_router_state() -> None:
        """按最新配置同步运行态；开启时顺带后台预热重资产（幂等）。"""
        enabled = bool(tier_service.get().get("enabled", False))
        _sync_smart_router_runtime(enabled)
        if enabled:
            prewarm_shared_assets()

    @router.get("/config", response_model=ConfigSnapshot)
    async def get_config_snapshot() -> ConfigSnapshot:
        return _snapshot()

    @router.post("/config/providers", response_model=ProviderView, status_code=status.HTTP_201_CREATED)
    async def create_provider(payload: ProviderCreateRequest) -> ProviderView:
        # ProviderConfigService.add() 不校验名称（它假定调用方已校验），而名称会
        # 变成 config.json 的键与派生环境变量名，必须在接口边界拦住。
        name_error = validate_name(payload.name)
        if name_error:
            raise ApiError(422, "invalid_provider_name", name_error)
        model_error = validate_model(payload.default_model)
        if model_error:
            raise ApiError(422, "invalid_model_name", model_error)
        _ensure_ok(
            provider_service.add(
                payload.name,
                payload.api_base,
                payload.default_model,
                payload.display_name,
                payload.api_key,
                context_window=payload.context_window,
                max_output_tokens=payload.max_output_tokens,
                max_tokens_field=payload.max_tokens_field,
                model_limits=(
                    {
                        name: item.model_dump(exclude_none=True)
                        for name, item in payload.model_limits.items()
                    }
                    if payload.model_limits is not None
                    else None
                ),
            )
        )
        if payload.set_base:
            _ensure_ok(provider_service.switch(payload.name, payload.default_model))
        return _provider_view(payload.name)

    @router.patch("/config/providers/{provider_name}", response_model=ProviderView)
    async def update_provider(provider_name: str, payload: ProviderUpdateRequest) -> ProviderView:
        if provider_service.get(provider_name) is None:
            raise ApiError(404, "provider_not_found", "Provider 不存在")
        fields: dict[str, Any] = {}
        if "api_base" in payload.model_fields_set and payload.api_base is not None:
            fields["api_base"] = payload.api_base
        if "default_model" in payload.model_fields_set and payload.default_model is not None:
            model_error = validate_model(payload.default_model)
            if model_error:
                raise ApiError(422, "invalid_model_name", model_error)
            fields["default_model"] = payload.default_model
        if "display_name" in payload.model_fields_set:
            fields["display_name"] = payload.display_name
        # 能力上限：只在显式提交时改动（`extra="forbid"` 已挡住拼错的键名）。
        # model_limits 是整表覆盖语义——空对象即清空覆盖（方案 §4.1）。
        if "context_window" in payload.model_fields_set and payload.context_window is not None:
            fields["context_window"] = payload.context_window
        if "max_output_tokens" in payload.model_fields_set and payload.max_output_tokens is not None:
            fields["max_output_tokens"] = payload.max_output_tokens
        if "max_tokens_field" in payload.model_fields_set and payload.max_tokens_field is not None:
            fields["max_tokens_field"] = payload.max_tokens_field
        if "model_limits" in payload.model_fields_set and payload.model_limits is not None:
            fields["model_limits"] = {
                name: item.model_dump(exclude_none=True)
                for name, item in payload.model_limits.items()
            }
        if fields:
            _ensure_ok(provider_service.update(provider_name, fields))
        return _provider_view(provider_name)

    @router.delete("/config/providers/{provider_name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_provider(provider_name: str) -> Response:
        _ensure_ok(provider_service.remove(provider_name, yes=True))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/config/providers/{provider_name}/key", response_model=ProviderView)
    async def set_provider_key(provider_name: str, payload: ProviderKeyRequest) -> ProviderView:
        _ensure_ok(provider_service.set_api_key(provider_name, payload.api_key, overwrite=payload.overwrite))
        return _provider_view(provider_name)

    @router.post("/config/providers/{provider_name}/models", response_model=ProviderView)
    async def add_provider_model(provider_name: str, payload: ModelRequest) -> ProviderView:
        _ensure_ok(provider_service.add_model(provider_name, payload.model))
        return _provider_view(provider_name)

    @router.delete("/config/providers/{provider_name}/models", response_model=ProviderView)
    async def remove_provider_model(
        provider_name: str, model: str = Query(min_length=1, max_length=200)
    ) -> ProviderView:
        # 模型名用查询参数而不是路径段：像 meta-llama/Llama-3-8B 这类名字带斜杠，
        # 放进路径会被拆成多段。
        _ensure_ok(provider_service.remove_model(provider_name, model))
        return _provider_view(provider_name)

    @router.post("/config/active", response_model=ConfigSnapshot)
    async def set_active_provider(payload: ActiveProviderRequest) -> ConfigSnapshot:
        _ensure_ok(provider_service.switch(payload.provider, payload.model))
        # 手动优先接管：显式切换模型即关闭智能路由（与 TUI `/model` 的
        # `_disable_smart_router` 一致），否则下一轮路由会立刻覆盖用户刚选的模型。
        if tier_service.get().get("enabled"):
            tier_service.set_enabled(False)
        _apply_smart_router_state()
        return _snapshot()

    @router.put("/config/tiers/{tier_name}", response_model=ConfigSnapshot)
    async def set_tier(tier_name: str, payload: TierRequest) -> ConfigSnapshot:
        _ensure_ok(tier_service.set_tier(tier_name, payload.provider, payload.model))
        # 配置档位会自动开启总闸：同步已存在会话并在开启时预热。
        _apply_smart_router_state()
        return _snapshot()

    @router.delete("/config/tiers/{tier_name}", response_model=ConfigSnapshot)
    async def clear_tier(tier_name: str) -> ConfigSnapshot:
        _ensure_ok(tier_service.clear_tier(tier_name))
        return _snapshot()

    @router.post("/config/smart-router", response_model=ConfigSnapshot)
    async def set_smart_router(payload: SmartRouterRequest) -> ConfigSnapshot:
        _ensure_ok(tier_service.set_enabled(payload.enabled))
        # 点击开启：立即对已存在会话生效，并在后台预热重资产；关闭则不加载。
        _apply_smart_router_state()
        return _snapshot()

    # ---------- 桌面端专用：运行时白名单授权 ----------

    def skill_registry_for(project_id: str | None) -> Any | None:
        """按项目上下文构造只读 Skill 注册表；无 project_id 时退化为 builtin + 用户级。"""
        from routivus.config.settings import load_settings

        config_manager = _build_config_manager(resolved_config)
        if project_id:
            project = require_project(project_id)
            project_root = Path(project.root_path).resolve()
        else:
            project_root = Path(resolved_config.user_dir).expanduser()
        return _build_skill_registry(
            config_manager, project_root, load_settings(config_manager), None
        )

    def skill_views(project_id: str | None) -> list[SkillView]:
        registry = skill_registry_for(project_id)
        if registry is None:
            return []
        return [_skill_view(info) for info in registry.list()]

    def set_skill_enabled(name: str, enabled: bool, project_id: str | None) -> list[SkillView]:
        registry = skill_registry_for(project_id)
        if registry is None or registry.get(name) is None:
            raise ApiError(404, "skill_not_found", f"未找到 Skill：{name}")
        if not registry.set_enabled(name, enabled):
            raise ApiError(422, "skill_update_failed", f"Skill 无法更新：{name}")
        return [_skill_view(info) for info in registry.list()]

    @router.get("/skills", response_model=list[SkillView])
    async def list_skills(project_id: str | None = None) -> list[SkillView]:
        """列出 builtin / 用户级 / 项目级 Skill（项目级需带 project_id）。

        只读元数据；正文由会话内的 `load_skill` 工具按需加载，避免把大文本塞进配置页。
        """
        return skill_views(project_id)

    @router.post("/skills/{name}/enable", response_model=list[SkillView])
    async def enable_skill(name: str, project_id: str | None = None) -> list[SkillView]:
        return set_skill_enabled(name, True, project_id)

    @router.post("/skills/{name}/disable", response_model=list[SkillView])
    async def disable_skill(name: str, project_id: str | None = None) -> list[SkillView]:
        return set_skill_enabled(name, False, project_id)

    def skill_detail(project_id: str | None, name: str) -> SkillDetail:
        """Skill 详情（含正文）：预览与编辑回填共用，只读无副作用。"""
        from routivus.skill.errors import SkillError
        from routivus.skill.parser import META_RE, read_body

        registry = skill_registry_for(project_id)
        info = registry.get(name) if registry is not None else None
        if registry is None or info is None:
            raise ApiError(404, "skill_not_found", f"未找到 Skill：{name}")
        body = ""
        try:
            text, _ = read_body(info.root / "SKILL.md", max_chars=registry.config.max_skill_chars)
            body = META_RE.sub("", text, count=1).strip()
        except SkillError:
            body = ""
        editable = info.source in {"user", "project"}
        return SkillDetail(
            **_skill_view(info).model_dump(),
            body=body,
            layer=info.source if editable else "",
            path=str(info.root / "SKILL.md"),
            editable=editable,
        )

    @router.get("/skills/{name}", response_model=SkillDetail)
    async def get_skill(name: str, project_id: str | None = None) -> SkillDetail:
        return skill_detail(project_id, name)

    @router.put("/skills/{name}", response_model=SkillDetail)
    async def write_skill(
        name: str, payload: SkillWriteBody, project_id: str | None = None
    ) -> SkillDetail:
        """新建 / 更新 SKILL.md（用户级或项目级）。

        安全边界：名称走 parser 契约，落点由 writer 二次校验（含符号链接），
        只允许 user / project 两层；内置 Skill 拒绝写入；同名已存在的 Skill 一律
        写回它所在的层，避免出现“影子副本”。
        """
        from routivus.config.settings import load_settings
        from routivus.skill.errors import SkillError
        from routivus.skill.writer import SkillWriteRequest, write_skill_document

        config_manager = _build_config_manager(resolved_config)
        settings = load_settings(config_manager)
        project_root: Path | None = None
        if project_id:
            project = require_project(project_id)
            project_root = Path(project.root_path).resolve()

        registry = skill_registry_for(project_id)
        existing = registry.get(name) if registry is not None else None
        if existing is not None and existing.source == "builtin":
            raise ApiError(422, "skill_readonly", "内置 Skill 不可编辑")
        layer = existing.source if existing is not None else payload.layer
        try:
            path, created = write_skill_document(
                SkillWriteRequest(
                    name=name, body=payload.body, description=payload.description, layer=layer
                ),
                user_dir=Path(config_manager.user_dir),
                project_root=project_root,
                max_chars=int(getattr(settings, "skills_max_chars", 32_000)),
            )
        except SkillError as exc:
            raise ApiError(422, "invalid_skill", str(exc)) from exc
        logger.info("skill written name=%s created=%s path=%s", name, created, path)
        return skill_detail(project_id, name)

    @router.get("/desktop/info", response_model=DesktopInfoResponse)
    async def desktop_info() -> DesktopInfoResponse:
        return DesktopInfoResponse(
            desktop=_desktop_mode(),
            user_dir=str(resolved_config.user_dir),
            legacy_user_dir=_legacy_user_dir(resolved_config),
            static_dir=str(resolved_config.static_dir) if resolved_config.static_dir else None,
            allowed_roots=project_registry.allowed_root_strings(),
        )

    @router.post("/desktop/roots", response_model=DesktopInfoResponse)
    async def grant_workspace_root(payload: RootGrantRequest) -> DesktopInfoResponse:
        """把用户在原生目录选择器里点选的目录加入允许的工作区根。

        仅本次运行有效（不落盘）：每次新增项目都要重新点选，避免"授权一次、
        永久放宽"。调用方须先持有访问令牌，外部进程无法冒充界面授权。
        """
        try:
            project_registry.allow_root(payload.path)
        except (UnsafeProjectPathError, ValueError) as exc:
            raise ApiError(422, "invalid_workspace_root", str(exc)) from exc
        return await desktop_info()

    running_tasks: dict[str, Any] = app.state.running_tasks
    session_agents: dict[str, Any] = app.state.session_agents
    connections: dict[str, list[WebSocket]] = app.state.ws_connections
    session_approvals: dict[str, ApprovalBridge] = app.state.session_approvals
    session_reviews: dict[str, PlanReviewBridge] = app.state.session_reviews
    session_routers: dict[str, SessionRouter | None] = app.state.session_routers
    project_memories: dict[str, MemoryManager] = app.state.project_memories
    terminals: dict[str, TerminalSession] = app.state.terminals

    def approval_bridge(session_id: str) -> ApprovalBridge:
        """会话级审批桥接。跨轮复用，使「本会话放行」不会在一轮结束后失效。"""
        existing = session_approvals.get(session_id)
        if existing is not None:
            return existing

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            current = workspace_store.get_session(session_id)
            if current is None:
                return
            # websocket=None → 广播给该会话的所有连接，多标签页都能看到审批卡。
            await send_event(None, event_type, current, data)

        async def set_waiting(waiting: bool) -> None:
            current = workspace_store.get_session(session_id)
            if current is None:
                return
            if waiting:
                status = "waiting_approval"
            elif current.status == "waiting_approval":
                # 只在仍处于等待态时恢复 running；会话已被取消/结束就不动它。
                status = "running"
            else:
                return
            updated = workspace_store.update_session(session_id, status=status)
            if updated is not None:
                await send_event(None, "session.status", updated, {"status": status})

        bridge = ApprovalBridge(
            emit=emit,
            set_waiting=set_waiting,
            timeout=resolved_config.approval_timeout,
        )
        session_approvals[session_id] = bridge
        return bridge

    def plan_review_bridge(session_id: str) -> PlanReviewBridge:
        """会话级计划审阅桥接。跨轮复用，队列语义由执行器保证（同时只有一个计划）。"""
        existing = session_reviews.get(session_id)
        if existing is not None:
            return existing

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            current = workspace_store.get_session(session_id)
            if current is None:
                return
            # websocket=None → 广播给该会话的所有连接，多标签页都能看到审阅卡。
            await send_event(None, event_type, current, data)

        bridge = PlanReviewBridge(
            emit=emit,
            view=lambda plan, mode: _plan_card_view(plan, mode=mode),
            timeout=resolved_config.approval_timeout,
        )
        session_reviews[session_id] = bridge
        return bridge

    def attach_connection(session_id: str, websocket: WebSocket) -> None:
        connections.setdefault(session_id, []).append(websocket)

    def detach_connection(session_id: str, websocket: WebSocket) -> None:
        peers = connections.get(session_id, [])
        connections[session_id] = [peer for peer in peers if peer is not websocket]
        if not connections[session_id]:
            connections.pop(session_id, None)

    async def send_event(
        websocket: WebSocket | None,
        event_type: str,
        session: SessionRecord,
        data: dict[str, Any] | None = None,
        *,
        persist: bool = True,
    ) -> None:
        # persist=False：只广播、不落库（见 `_EPHEMERAL_EVENT_TYPES`）。高频流式增量
        # 走这条路，省掉"每条一次完整事务"——它们占了事件表 73% 的行数，且没有读者。
        payload = (
            await emit(event_type, session, data)
            if persist
            else _ephemeral_payload(event_type, session, data)
        )
        peers = list(connections.get(session.id, []))
        if not peers and websocket is not None:
            peers = [websocket]
        for peer in peers:
            try:
                await peer.send_json(payload)
            except Exception:
                detach_connection(session.id, peer)

    def websocket_authorized(websocket: WebSocket) -> bool:
        expected = resolved_config.ws_auth_token
        if not expected:
            return True
        authorization = websocket.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        token = token or websocket.query_params.get("token", "")
        return token == expected

    def websocket_origin_allowed(websocket: WebSocket) -> bool:
        if not resolved_config.allowed_origins:
            return True
        origin = websocket.headers.get("origin", "").strip()
        return not origin or origin in resolved_config.allowed_origins

    def request_is_new(session_id: str, request_id: str) -> bool:
        return workspace_store.claim_request(session_id, request_id)

    def _cancel_bridges(session_id: str) -> None:
        """取消一轮时解开所有挂起交互：审批按拒绝、提问按跳过、审阅按取消。"""
        approval = session_approvals.get(session_id)
        if approval is not None:
            approval.cancel_pending()
        review = session_reviews.get(session_id)
        if review is not None:
            review.cancel_pending()

    async def ensure_session_agent(project: Any, session: SessionRecord) -> Any | None:
        """取回（或惰性创建）会话级 agent；agent 工厂缺失时返回 None。"""
        factory = app.state.agent_factory
        if factory is None:
            return None
        agent = session_agents.get(session.id)
        if agent is None:
            agent = factory(project, session)
            if inspect.isawaitable(agent):
                agent = await agent
            session_agents[session.id] = agent
        return agent

    def bind_interactions(agent: Any, session_id: str) -> None:
        """agent 在会话内跨轮复用，所以每轮都重挂一次交互回调。

        getattr 是为了兼容测试/嵌入方注入的简易 agent。
        """
        bridge = approval_bridge(session_id)
        policy = getattr(agent, "approval_policy", None)
        if policy is not None:
            policy.requester = bridge.request
        if hasattr(agent, "ask_requester"):
            agent.ask_requester = bridge.ask

    def session_router(session_id: str) -> SessionRouter | None:
        """会话级 SmartRouter 运行态（惰性创建）。

        构造失败只降级为「该会话不路由」并记住结论 —— 每轮重试既无意义，
        也会把同一条告警刷满日志。
        """
        if session_id in session_routers:
            return session_routers[session_id]
        router: SessionRouter | None
        try:
            router = SessionRouter(session_key=session_id)
        except Exception:
            logger.warning("smart router 初始化失败，该会话不做路由 session_id=%s", session_id, exc_info=True)
            router = None
        session_routers[session_id] = router
        return router

    def router_snapshot(session_id: str) -> dict[str, Any]:
        """会话快照里的路由状态：优先用最近一次路由结果，否则回落到开关状态。"""
        session_router = session_routers.get(session_id)
        result = getattr(session_router, "last", None)
        if result is not None:
            return router_payload(result, meta=getattr(session_router, "last_meta", None))
        enabled = False
        try:
            enabled = bool(tier_service.get().get("enabled"))
        except Exception:  # pragma: no cover - 配置损坏时不该挡住快照
            logger.debug("smart router state unavailable", exc_info=True)
        return {"enabled": enabled, "tier": "", "provider": "", "model": ""}

    def project_memory(project_root: Any) -> MemoryManager | None:
        """取项目长期记忆库（只读视图用）；取不到返回 None。

        优先复用该会话 agent 已打开的实例（`memory_snapshot` 负责）。走到这里说明
        agent 还没建：只在 `<root>/.routivus/memory.db` **已存在**时才打开——看一眼
        记忆不该在用户的项目里凭空建出数据库文件（没库＝没记忆过，返回 None 即可）。
        按项目根缓存句柄，避免每次请求重复初始化 SQLite。
        """
        root = Path(str(project_root))
        key = str(root)
        if key in project_memories:
            return project_memories[key]
        if not (root / ".routivus" / "memory.db").exists():
            return None
        try:
            manager = MemoryManager(root)
        except Exception:  # noqa: BLE001 - 库损坏 / 权限不足都不该打断界面
            logger.warning("长期记忆库打开失败 project_root=%s", root, exc_info=True)
            return None
        project_memories[key] = manager
        return manager

    def memory_snapshot(session: SessionRecord, project: Any, limit: int = 20) -> dict[str, Any]:
        """会话快照 / 记忆端点共用的载荷：会话所属项目的长期记忆条目。

        记忆是项目级数据，与"哪个会话"无关；走 agent 的实例只是为了复用同一个
        已打开的库句柄（agent 未创建时按需读取磁盘上的库，见 project_memory）。
        """
        agent = session_agents.get(session.id)
        memory = getattr(agent, "memory_manager", None) or project_memory(getattr(project, "root_path", ""))
        return memory_payload(project.id, memory, limit=limit)

    def context_payload(session: SessionRecord, agent: Any | None = None) -> dict[str, Any]:
        """当前生效的能力上限（会话快照 / `context.updated` 事件共用）。

        优先取 agent.settings —— 每次 `_attach_model` 之后它都是最新解析结果，所以
        `/model` 切模型与 SmartRouter 换档都会让它变化（方案 §4.4）。agent 还没创建
        （只是打开会话）时按会话记录里的 provider/model 现算，保证界面一打开就能看到
        真数，而不是等下一轮对话。

        三个数一起返回：window 与 max_output 同源、同时变，分开下发会让界面出现
        「新窗口 + 旧输出上限」的中间态。
        """
        settings = getattr(agent, "settings", None) if agent is not None else None
        provider_name = str(
            getattr(settings, "provider", "") or getattr(session, "active_provider", "") or ""
        )
        model = str(
            getattr(settings, "model", "") or getattr(session, "active_model", "") or ""
        )
        window = int(getattr(settings, "context_window", 0) or 0)
        max_output = int(getattr(settings, "max_output_tokens", 0) or 0)
        output_field = str(
            getattr(settings, "max_tokens_field", DEFAULT_MAX_TOKENS_FIELD) or ""
        )
        source = "provider"
        manager = getattr(agent, "config_manager", None) if agent is not None else None
        manager = manager or config_manager
        if not provider_name:
            # 会话记录里可能还没写 provider（新建会话、agent 尚未创建）：回落到
            # 当前生效的 base provider，保证「刚打开会话」看到的也是真数。
            try:
                active = manager.active()
            except Exception:  # noqa: BLE001 - 未配置 provider 属正常初始状态
                active = None
            if active is not None:
                provider_name = active.provider_name
                model = model or active.model
        provider = None
        resolver = getattr(manager, "resolve_provider", None)
        if callable(resolver) and provider_name:
            try:
                provider = resolver(provider_name)
            except Exception:  # noqa: BLE001 - 只读展示字段，不能影响对话
                provider = None
        if provider is not None:
            try:
                resolved, source = manager.resolve_window_detail(provider, model)
                window = window or resolved
                max_output = max_output or manager.resolve_output_limit(provider, model)
                if settings is None:
                    output_field = manager.resolve_output_field(provider)
            except Exception:  # noqa: BLE001 - 同上，失败就退回 settings 上的现成值
                pass
        elif window <= 0:
            window, source = 128_000, "default"
        return {
            "window": window,
            "max_output": max_output,
            "output_field": output_field,
            "provider": provider_name,
            "model": model,
            "source": source,
        }

    async def apply_smart_routing(
        forwarder: "_TurnForwarder", agent: Any, content: str, session: SessionRecord
    ) -> None:
        """普通对话轮：开关开启时按复杂度换档，并把结果推给客户端。

        只在普通轮调用（`/plan`、`/team` 不路由，与 TUI 的门禁一致）。路由失败
        或换模型失败都不阻断对话本身——最差就是沿用当前模型。
        """
        settings = getattr(agent, "settings", None)
        if not getattr(settings, "smart_router_enabled", False):
            return
        manager = getattr(agent, "config_manager", None)
        if manager is None:
            # 测试 / 嵌入方注入的简易 agent：没有 ConfigManager 就无法解析四档的
            # provider 与 API Key，静默跳过而不是每轮报错。
            return

        # 路由含同步重活（首次要加载 20+ MB 的 ML / 语义模型），必须丢到工作线程：
        # 在事件循环里直接跑会把整个 uvicorn 卡住（心跳、其它 HTTP 全停响应），
        # 表现就是"一对话就卡住"。超时只降级为「本轮不换档」，不阻断对话。
        timeout = route_timeout_seconds()
        try:
            router = await asyncio.wait_for(
                asyncio.to_thread(session_router, session.id), timeout=timeout
            )
            if router is None:
                await forwarder.emit("router.updated", {
                    "enabled": True,
                    "tier": "", "provider": "", "model": "",
                    "error": "智能路由初始化失败，本轮沿用当前模型",
                })
                return
            result, error = await asyncio.wait_for(
                asyncio.to_thread(
                    router.apply, content, settings=settings, manager=manager, agent=agent
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "smart router 超时（>%.0fs） session_id=%s（本轮沿用当前模型）", timeout, session.id
            )
            await forwarder.emit("router.updated", {
                "enabled": True,
                "tier": "", "provider": "", "model": "",
                "error": f"智能路由超时（>{timeout:.0f}s），本轮沿用当前模型",
            })
            return
        except Exception as exc:
            logger.warning("smart router 路由失败 session_id=%s", session.id, exc_info=True)
            await forwarder.emit("router.updated", {
                "enabled": True,
                "tier": "", "provider": "", "model": "",
                "error": f"路由失败，本轮沿用当前模型：{exc}",
            })
            return
        await forwarder.emit(
            "router.updated",
            router_payload(result, error, meta=getattr(router, "last_meta", None)),
        )
        # 用量归因（方案 12 §4.2）：路由结果比 settings 更准（换档可能连 provider 一起换），
        # 所以放在这里覆盖。它只影响 session.usage 事件的附加字段，不参与任何判定。
        forwarder.set_attrs(tier=result.tier, provider=result.provider, model=result.model)
        # 路由换档可能连 provider 一起换掉：窗口与输出上限跟着变，必须同步给界面，
        # 否则「使用率」会一直按上一档的分母算（方案 §4.4 最容易漏的一处）。
        await forwarder.emit("context.updated", context_payload(session, agent))

    def model_attrs(agent: Any) -> dict[str, str]:
        """本轮实际使用的 provider / model。

        智能路由换档会改 `agent.settings`（`_attach_model` 里），所以这里取到的就是
        "这一轮真正在用的那个"；未路由的轮（/plan、/team）也照样填得上。
        """
        settings = getattr(agent, "settings", None)
        return {
            "provider": str(getattr(settings, "provider", "") or ""),
            "model": str(getattr(settings, "model", "") or ""),
        }

    class _TurnForwarder:
        """一轮执行的共享转发器：把 agent / 计划 / 团队事件映射为会话事件。

        内容增量、工具生命周期、用量、错误这几类映射对 ReAct 轮与计划 / 团队的
        子任务同样适用，所以三者共用一份映射，避免三处各自漂移。
        """

        def __init__(self, websocket: WebSocket | None, session: SessionRecord, request_id: str) -> None:
            self.websocket = websocket
            self.session = session
            self.request_id = request_id
            # 段缓冲按来源分桶（07 §4.2）：/team 的同一批 worker 是并发执行的
            # （agent/team.py 的 create_task + 信号量），delta 会交替到达；单一缓冲
            # 会把不同 worker 的文字黏成一段。主 ReAct 轮的来源是 ""。
            self.segments: dict[str, dict[str, Any]] = {}
            self.failed = False
            self.tool_started: dict[str, float] = {}
            self.tool_calls = 0
            self.tool_failures = 0
            # 本轮实际使用的 provider / model / 档位：并进 session.usage 事件，供首页
            # token 面板按档位归因（方案 12 §4.2）。老事件没有这几个键 → 归入「未标注」，
            # 不猜也不回填。
            self.attrs: dict[str, str] = {}

        def set_attrs(self, **kwargs: str) -> None:
            """补归因字段：只覆盖非空值，后设的优先（路由结果要盖掉 settings 的初值）。"""
            self.attrs.update({key: str(value) for key, value in kwargs.items() if value})

        def usage_payload(self, usage: Any, item: Any | None = None) -> dict[str, Any]:
            """用量事件载荷 = usage 三件套 + 本轮归因字段 + 上下文预算。

            预算三件套（`estimated_prompt_tokens` / `request_token_limit` /
            `context_window`）来自 `react.py` 的 context_fields，界面靠它显示"下一次
            请求有多大、离压缩触发线还有多远"。

            不透出去的话，界面只剩「累计用量 ÷ 窗口」可算——那是把**整个会话所有请求
            的总和**除以**单次请求的上限**，会话跑得越久越接近 100%，跟"要不要压缩"
            毫无关系（每一轮都要把历史重发一遍，累计必然虚高）。
            """
            payload: dict[str, Any] = {**_record_payload(usage), **self.attrs}
            if item is None:
                return payload
            for key in ("estimated_prompt_tokens", "request_token_limit", "context_window"):
                value = getattr(item, key, None)
                if value is not None:
                    payload[key] = int(value)
            return payload

        async def emit(self, event_type: str, data: dict[str, Any]) -> None:
            # 流式增量只广播、不落库（Optimization 01 §5.1）：它占事件表条数的 73%，
            # 却没有任何读取方——不在重连回放类型里，前端也只在 WS 上实时消费。
            await send_event(
                self.websocket,
                event_type,
                self.session,
                {**data, "request_id": self.request_id},
                persist=event_type not in _EPHEMERAL_EVENT_TYPES,
            )

        async def _flush_segment(self, source: str) -> None:
            """把某来源的当前段收尾：落库 + 广播 message.segment（先落库、后发事件）。

            "先落库"保证消息的 created_at ≤ 事件 occurred_at，前端按时间戳归并时
            段落在先（方案 07 §4.6a 的硬性约定）。

            空白段（多数 provider 会在段边界吐空白）不落库，但**仍要通知前端**：
            否则前端那条活跃段永远收不掉，界面上会永久留一个空块（形似一条横线）。
            """
            segment = self.segments.pop(source, None)
            if not segment:
                return
            text = "".join(segment["parts"])
            if not text.strip():
                await self.emit("message.segment", {"message": None, "source": source})
                return
            if segment["kind"] == "thinking" and len(text) > MAX_THINKING_SEGMENT_CHARS:
                text = text[:MAX_THINKING_SEGMENT_CHARS] + _THINKING_TRUNCATED_SUFFIX
            role = "thinking" if segment["kind"] == "thinking" else "assistant"
            message = workspace_store.add_message(self.session.id, role, text)
            await self.emit("message.segment", {
                "message": _record_payload(_message_response(message)),
                "source": source,
            })

        async def _append_text(self, source: str, kind: str, text: str) -> None:
            segment = self.segments.get(source)
            if segment is None or segment["kind"] != kind:
                await self._flush_segment(source)
                self.segments[source] = {"kind": kind, "parts": [text]}
            else:
                segment["parts"].append(text)

        def apply_usage(self, usage: Any) -> None:
            updated = workspace_store.update_session(
                self.session.id,
                prompt_tokens=self.session.prompt_tokens + max(0, int(getattr(usage, "prompt_tokens", 0))),
                completion_tokens=self.session.completion_tokens + max(0, int(getattr(usage, "completion_tokens", 0))),
                total_tokens=self.session.total_tokens + max(0, int(getattr(usage, "total_tokens", 0))),
            )
            if updated is not None:
                self.session = updated

        async def forward(self, item: Any, *, source: str = "") -> None:
            """把一个 AgentEvent 映射为会话事件。

            `source` 标注事件来源（""=主 ReAct 轮，task:<id>=/plan 子任务，
            agent:<id>=/team worker），用于段缓冲分桶与前端来源标注（07 §4.2/§4.10）。
            """
            kind = getattr(item, "kind", "")
            text = str(getattr(item, "text", "") or "")
            if kind in {"content", "thinking"} and text:
                await self._append_text(source, kind, text)
                await self.emit("message.delta", {"kind": kind, "text": text, "source": source})
            elif kind == "tool_call":
                # 工具卡必须插在"它之前的正文"后面：先收尾该来源的段，再发 tool.started
                await self._flush_segment(source)
                call = getattr(item, "tool_call", None)
                call_id = str(getattr(call, "id", ""))
                self.tool_started[call_id] = time.perf_counter()
                self.tool_calls += 1
                await self.emit("tool.started", {
                    "tool_call_id": call_id,
                    "name": getattr(call, "name", ""),
                    "arguments": getattr(call, "arguments", ""),
                    "source": source,
                })
            elif kind == "tool_result":
                result = getattr(item, "tool_result", None)
                result_id = str(getattr(result, "tool_call_id", ""))
                ok = bool(getattr(result, "ok", False))
                if not ok:
                    self.tool_failures += 1
                output = getattr(result, "output", "")
                error = getattr(result, "error", "")
                await self.emit("tool.completed", {
                    "tool_call_id": result_id,
                    "name": getattr(result, "name", ""),
                    "ok": ok,
                    "output": output,
                    "error": error,
                    "duration_ms": round((time.perf_counter() - self.tool_started.pop(result_id, time.perf_counter())) * 1000),
                    "source": source,
                })
                workspace_store.add_message(
                    self.session.id, "tool", output or error or "",
                    tool_name=str(getattr(result, "name", "") or ""),
                    tool_result=output or error,
                )
                await self.emit("audit.updated", {
                    "tool_calls": self.tool_calls,
                    "tool_failures": self.tool_failures,
                })
            # kind == "approval"（审批决策的通知）**不再转发**：ApprovalBridge 已经发过
            # 带 `approval_id` 的 `approval.resolved`（那才是正主），这里再发一份只有
            # `tool_call_id` 的同名事件，前端按 id 匹配不上就会**无条件清空当前审批卡**
            # ——并行 worker 时，A 的决策会把 B 正等着用户点的卡直接抹掉。信息上它也
            # 是 ApprovalBridge 事件的子集，转发纯属冗余。
            elif kind == "ask_user":
                await self.emit("approval.requested", {
                    "ask": _record_payload(getattr(item, "ask", None)),
                })
            elif kind in {"usage", "context_usage"}:
                usage = getattr(item, "usage", None)
                if usage:
                    self.apply_usage(usage)
                    await self.emit("session.usage", self.usage_payload(usage, item))
            elif kind in {"context_warning", "context_compacted", "context_overflow", "budget_exceeded"}:
                await self.emit("memory.updated", {"kind": kind, "message": text})
            elif getattr(item, "plan", None) is not None or kind.startswith("plan_"):
                await self.emit("plan.updated", _task_card_payload(item, mode="plan"))
            elif getattr(item, "team_id", "") or kind.startswith("team_") or kind.startswith("task_") or kind in {"batch_started", "subtask_started", "subtask_done", "subtask_failed"}:
                await self.emit("team.updated", _task_card_payload(item, mode="team"))
            elif kind == "error":
                self.failed = True
                await self.emit("error", {"code": "agent_error", "message": text})
            elif kind == "done":
                usage = getattr(item, "usage", None)
                if usage:
                    self.apply_usage(usage)
                    await self.emit("session.usage", self.usage_payload(usage, item))

        async def finish(self) -> None:
            """一轮正常结束：收尾所有来源的段落并收敛会话状态。

            正文 / 思考已由 `message.segment` 逐段落库，这里只发"本轮结束"信号；
            `message.completed` 不再携带整段正文（语义收窄，方案 07 §4.3）。
            """
            for source in list(self.segments):
                await self._flush_segment(source)
            await self.emit("message.completed", {})
            updated = workspace_store.update_session(self.session.id, status="failed" if self.failed else "completed")
            if updated:
                self.session = updated
                await self.emit("session.status", {"status": updated.status})

        async def close_with(self, status: str, *, error: str = "", code: str = "agent_error") -> None:
            """一轮提前结束（参数错误、依赖缺失等）：报错并把会话状态收敛到 status。"""
            if error:
                await self.emit("error", {"code": code, "message": error})
            updated = workspace_store.update_session(self.session.id, status=status)
            if updated:
                self.session = updated
                await self.emit("session.status", {"status": status})

    async def forward_task_event(item: Any, forwarder: _TurnForwarder, *, mode: str) -> None:
        """计划 / 团队事件 → 卡片事件；子任务内部事件复用 AgentEvent 映射。

        `subtask_event` 携带的是子任务内部的 AgentEvent（内容 / 思考 / 工具调用），
        按会话事件转发后用户才能在计划执行期间看到实际进展，而不是只有状态跳变。
        """
        event_type = "plan.updated" if mode == "plan" else "team.updated"
        await forwarder.emit(event_type, _task_card_payload(item, mode=mode))
        inner = getattr(item, "agent_event", None)
        if inner is not None:
            # 段缓冲按来源分桶（07 §4.2）：/team 并行 worker 用 agent:<id>，
            # /plan 子任务用 task:<id>，避免不同来源的文字黏成一段。
            agent_id = str(getattr(item, "agent_id", "") or "")
            task_id = str(getattr(getattr(item, "task", None), "id", "") or "")
            if agent_id:
                source = f"agent:{agent_id}"
            elif task_id:
                source = f"task:{task_id}"
            else:
                source = "subtask"
            await forwarder.forward(inner, source=source)
        usage = getattr(item, "usage", None)
        if usage is not None:
            forwarder.apply_usage(usage)
            await forwarder.emit("session.usage", forwarder.usage_payload(usage))

    def _executor_kwargs(agent: Any) -> dict[str, Any]:
        """计划 / 团队执行器与 ReAct 共用的依赖（都挂在 agent 上）。"""
        return {
            "llm": agent.llm,
            "tools": agent.tools,
            "settings": agent.settings,
            "approval_policy": getattr(agent, "approval_policy", None),
            "audit": getattr(agent, "audit", None),
            "memory_manager": getattr(agent, "memory_manager", None),
            "mcp_manager": getattr(agent, "mcp_manager", None),
            "ask_requester": getattr(agent, "ask_requester", None),
        }

    def _missing_executor_deps(agent: Any) -> list[str]:
        """计划 / 团队执行器的必需依赖；测试或嵌入方注入的简易 agent 可能没有。"""
        return [name for name in ("llm", "tools", "settings") if getattr(agent, name, None) is None]

    async def handle_turn_cancelled(websocket: WebSocket | None, session: SessionRecord, request_id: str) -> None:
        _cancel_bridges(session.id)
        updated = workspace_store.update_session(session.id, status="cancelled")
        if updated:
            try:
                await send_event(websocket, "session.status", updated, {"status": "cancelled", "request_id": request_id})
            except Exception:
                pass

    async def handle_turn_failure(
        websocket: WebSocket | None, session: SessionRecord, request_id: str, exc: Exception, *, label: str
    ) -> None:
        logger.exception("%s failed session_id=%s", label, session.id)
        updated = workspace_store.update_session(session.id, status="failed")
        try:
            await send_event(websocket, "error", session, {"code": "agent_error", "message": str(exc), "request_id": request_id})
            if updated:
                await send_event(websocket, "session.status", updated, {"status": "failed", "request_id": request_id})
        except Exception:
            pass

    async def run_agent_turn(websocket: WebSocket | None, project: Any, session: SessionRecord, content: str, request_id: str = "") -> None:
        forwarder = _TurnForwarder(websocket, session, request_id)
        try:
            agent = await ensure_session_agent(project, session)
            if agent is None:
                await forwarder.close_with(
                    "failed", error="Agent 尚未配置", code="agent_unavailable"
                )
                return
            bind_interactions(agent, session.id)
            # 用量归因（方案 12 §4.2）：先记 settings 里的 provider/model，路由成功后再
            # 由 apply_smart_routing 补 tier 并覆盖成"本轮实际用的那个"。
            forwarder.set_attrs(**model_attrs(agent))
            # 普通轮在执行前路由换档（/plan、/team 不路由，与 TUI 一致）
            await apply_smart_routing(forwarder, agent, content, session)
            # `@` 文件引用（方案 05 §4.1）：只有**发给模型的这一份**带提示段——落库、
            # 路由、消息流仍用原文。提示段只含"提到了哪些路径、在不在"，不含内容。
            prompt = content
            try:
                hint = reference_hint(Path(project.root_path), content)
            except Exception:  # noqa: BLE001 - 提示段是增益，失败不该拦住这一轮
                hint = ""
            if hint:
                prompt = f"{content}\n\n{hint}"
            stream = agent.run(prompt)
            if inspect.isawaitable(stream):
                stream = await stream
            async for item in stream:
                await forwarder.forward(item)
            await forwarder.finish()
        except asyncio.CancelledError:
            await handle_turn_cancelled(websocket, session, request_id)
            raise
        except Exception as exc:
            await handle_turn_failure(websocket, session, request_id, exc, label="agent turn")

    async def run_plan_turn(websocket: WebSocket | None, project: Any, session: SessionRecord, goal: str, request_id: str = "") -> None:
        """`/plan <任务>`：拆解 → 审阅 → 按依赖批次执行。"""
        forwarder = _TurnForwarder(websocket, session, request_id)
        try:
            agent = await ensure_session_agent(project, session)
            if agent is None:
                await forwarder.close_with("failed", error="Agent 尚未配置", code="agent_unavailable")
                return
            missing = _missing_executor_deps(agent)
            if missing:
                await forwarder.close_with(
                    "failed",
                    error=f"当前 Agent 不支持计划模式（缺少 {', '.join(missing)}）",
                    code="plan_unavailable",
                )
                return
            bind_interactions(agent, session.id)
            forwarder.set_attrs(**model_attrs(agent))  # /plan 不路由，只有 provider/model
            review = plan_review_bridge(session.id)
            review.mode = "plan"

            from routivus.agent.plan import PlanExecutor

            executor = PlanExecutor(**_executor_kwargs(agent), reviewer=review.review)
            async for event in executor.run(goal):
                await forward_task_event(event, forwarder, mode="plan")
            await forwarder.finish()
        except asyncio.CancelledError:
            await handle_turn_cancelled(websocket, session, request_id)
            raise
        except Exception as exc:
            await handle_turn_failure(websocket, session, request_id, exc, label="plan turn")

    def _write_team_snapshot(session_id: str, project_id: str, data: dict[str, Any]) -> None:
        """把执行器的可恢复快照落库（方案 15 §4.2）。

        快照只是"能续跑"的附加能力：写失败只记日志，绝不打断正在跑的任务
        （执行器那一侧也已经兜了异常）。
        """
        try:
            workspace_store.append_event(session_id, project_id, _TEAM_SNAPSHOT_EVENT, data)
        except Exception:  # noqa: BLE001 - 快照失败不影响任务
            logger.warning("team snapshot 落库失败 session_id=%s", session_id, exc_info=True)

    async def run_team_turn(websocket: WebSocket | None, project: Any, session: SessionRecord, goal: str, request_id: str = "") -> None:
        """`/team <任务>`：Supervisor 调度隔离 Worker，并对结果做证据化审查。"""
        forwarder = _TurnForwarder(websocket, session, request_id)
        try:
            agent = await ensure_session_agent(project, session)
            if agent is None:
                await forwarder.close_with("failed", error="Agent 尚未配置", code="agent_unavailable")
                return
            missing = _missing_executor_deps(agent)
            if missing:
                await forwarder.close_with(
                    "failed",
                    error=f"当前 Agent 不支持团队模式（缺少 {', '.join(missing)}）",
                    code="team_unavailable",
                )
                return
            bind_interactions(agent, session.id)
            forwarder.set_attrs(**model_attrs(agent))  # /team 不路由，只有 provider/model
            review = plan_review_bridge(session.id)
            review.mode = "team"

            from routivus.agent.team import TeamExecutor

            executor = TeamExecutor(
                **_executor_kwargs(agent),
                reviewer=review.review,
                project_root=Path(project.root_path),
                on_snapshot=lambda data: _write_team_snapshot(session.id, project.id, data),
            )
            async for event in executor.run(goal):
                await forward_task_event(event, forwarder, mode="team")
            await forwarder.finish()
        except asyncio.CancelledError:
            await handle_turn_cancelled(websocket, session, request_id)
            raise
        except Exception as exc:
            await handle_turn_failure(websocket, session, request_id, exc, label="team turn")

    async def run_team_resume_turn(
        websocket: WebSocket | None,
        project: Any,
        session: SessionRecord,
        payload: dict[str, Any],
        request_id: str = "",
    ) -> None:
        """团队卡上的「继续」（方案 15 §4.4）：从快照重建任务图接着跑，**不重新规划**。

        两种形态：
        - 不带 `scope`：续跑整轮（非权限类失败 / 取消）——done 跳过、其余重跑；
        - 带 `scope`：救那个"补个范围就能好"的任务，修好后自动接着跑剩余批次。

        写入范围由**用户勾选**产生，这里只做协议形状校验；资源策略校验（越界 / 黑名单 /
        只读升写）在执行器构造 Repairer 任务时再做一次——那是唯一进入执行的通路，
        fail closed。
        """
        forwarder = _TurnForwarder(websocket, session, request_id)
        try:
            agent = await ensure_session_agent(project, session)
            if agent is None:
                await forwarder.close_with("failed", error="Agent 尚未配置", code="agent_unavailable")
                return
            missing = _missing_executor_deps(agent)
            if missing:
                await forwarder.close_with(
                    "failed",
                    error=f"当前 Agent 不支持团队模式（缺少 {', '.join(missing)}）",
                    code="team_unavailable",
                )
                return

            from routivus.agent.team import TeamExecutor, team_plan_from_snapshot

            snapshots = workspace_store.list_card_events(session.id, (_TEAM_SNAPSHOT_EVENT,), limit=1)
            if not snapshots:
                await forwarder.close_with(
                    "idle",
                    error="没有可续跑的 Team 任务（需要先在本会话跑过一次 /team）",
                    code="no_resumable_team",
                )
                return
            data = snapshots[-1].data
            plan = team_plan_from_snapshot(data)
            if plan is None:
                await forwarder.close_with(
                    "idle", error="Team 快照不可解析，无法续跑", code="invalid_snapshot"
                )
                return

            limit = max(0, int(getattr(agent.settings, "task_max_resumes", 3)))
            resume_count = max(0, int(data.get("resume_count", 0) or 0))
            if resume_count >= limit:
                await forwarder.close_with(
                    "idle",
                    error=f"续跑次数已达上限（{limit} 次），请重新发起 /team",
                    code="resume_quota_exceeded",
                )
                return

            claims, scope_error = _parse_scope_claims(payload.get("scope"))
            if scope_error:
                await forwarder.close_with("idle", error=scope_error, code="invalid_scope")
                return
            instruction = str(payload.get("instruction", "") or "").strip()
            task_id = str(payload.get("task_id", "") or "").strip()

            bind_interactions(agent, session.id)
            forwarder.set_attrs(**model_attrs(agent))  # 续跑同样记一份归因
            executor = TeamExecutor(
                **_executor_kwargs(agent),
                # 续跑不重新规划，用不到计划审阅回调；任务级审查仍走 LLM（`_review` 的默认路径）。
                reviewer=None,
                project_root=Path(project.root_path),
                team_id=str(data.get("team_id", "") or "") or None,
                resume_plan=plan,
                resume_count=resume_count,
                on_snapshot=lambda snap: _write_team_snapshot(session.id, project.id, snap),
            )

            if claims:
                target = task_id or _first_needs_scope_task(plan)
                if not target:
                    await forwarder.close_with(
                        "idle",
                        error="没有等待确认写入范围的任务；如需续跑失败任务，请直接点「继续」",
                        code="no_pending_task",
                    )
                    return
                stream = executor.resume_task_with_repair_scope(target, claims)
            else:
                stream = executor.resume(instruction)

            async for event in stream:
                await forward_task_event(event, forwarder, mode="team")
            await forwarder.finish()
        except asyncio.CancelledError:
            await handle_turn_cancelled(websocket, session, request_id)
            raise
        except Exception as exc:
            await handle_turn_failure(websocket, session, request_id, exc, label="team resume turn")

    def _turn_coroutine(
        websocket: WebSocket | None,
        project: Any,
        session: SessionRecord,
        turn_kind: str,
        goal: str,
        options: dict[str, Any],
        content: str,
        request_id: str,
    ) -> Any:
        """按输入前缀选择回合执行器：`/plan`、`/team` 或普通对话。"""
        if turn_kind == "plan":
            return run_plan_turn(websocket, project, session, goal, request_id)
        if turn_kind == "team":
            return run_team_turn(websocket, project, session, goal, request_id)
        return run_agent_turn(websocket, project, session, content, request_id)

    async def perform_cancel(websocket: WebSocket | None, session: SessionRecord, request_id: str) -> SessionRecord:
        """取消当前任务：有运行中轮次就取消任务，否则把会话标记为 cancelled。

        供 `cancel` 消息与 `/cancel`、`/c` 命令别名共用（行为必须完全一致）。
        """
        _cancel_bridges(session.id)
        task = running_tasks.get(session.id)
        if task is not None and not task.done():
            task.cancel()
            return session
        updated = workspace_store.update_session(session.id, status="cancelled")
        if updated is not None:
            await send_event(websocket, "session.status", updated, {"status": "cancelled", "request_id": request_id})
            return updated
        return session

    async def run_command_turn(
        websocket: WebSocket | None, project: Any, session: SessionRecord, content: str, request_id: str = ""
    ) -> SessionRecord:
        """同步执行 slash 命令（复用 TUI 的 CommandService），回执落库并广播。

        与轮次的关键差异：不创建 running_tasks、不改会话状态、不触发智能路由。
        回执以 assistant 消息落库（重连可见），`command.executed` 事件附 ok 标记
        供前端着色。设计方案见 plans/enhancement/01-web-slash-commands.md。
        """
        from routivus.cli.commands import CommandContext, CommandService

        cmd = content.split(maxsplit=1)[0].lower()
        ok = False
        message = ""
        agent: Any | None = None
        if cmd in ("/exit", "/quit"):
            # Web 没有"退出进程"语义，/exit 已从命令通道移除：按未知命令处理。
            # 不能放行到 CommandService——其 /exit 返回 goodbye + should_exit。
            message = "未知命令：/exit（Web Console 无退出命令；直接关闭窗口即可）"
        else:
            try:
                agent = await ensure_session_agent(project, session)
            except Exception as exc:
                agent = None
                logger.warning("命令执行前创建 agent 失败 session_id=%s", session.id, exc_info=True)
                message = f"Agent 尚未就绪（{exc}）。请先在配置页完成 Provider 设置。"
            if agent is None:
                if not message:
                    message = "Agent 尚未配置。请先在配置页完成 Provider 设置再使用命令。"
            else:
                ctx = CommandContext(
                    agent=agent,
                    settings=getattr(agent, "settings", None),
                    manager=getattr(agent, "config_manager", None),
                )
                try:
                    result = await CommandService(ctx).execute(content)
                    message = result.message or "（无输出）"
                    ok = bool(result.ok)
                except Exception as exc:
                    logger.warning("命令执行失败 session_id=%s command=%s", session.id, cmd, exc_info=True)
                    message = f"命令执行失败：{exc}"
                # service 层的未知命令文案来自 TUI（列出全部 21 个命令，含 Web
                # 未接入的 /exit /init 等），换成 Web 白名单的一行清单避免误导。
                if not ok and message.startswith(("未知命令", "Unknown command")):
                    from routivus.server.completions import WEB_COMMANDS

                    available = " ".join(WEB_COMMANDS)
                    message = f'未知命令 "{cmd}"。可用命令：{available}，详见 /help'

        if cmd == "/clear" and ok:
            # /clear 只清 agent 的运行上下文；已落库消息仍会随快照重建展示。
            message += "\n（说明：仅清空模型的运行上下文；会话记录仍保留在消息流中）"

        # 命令输入本身也落库：重连后能看出"当时敲了什么命令"。
        user_message = workspace_store.add_message(session.id, "user", content)
        await send_event(websocket, "message.created", session, {"message": _record_payload(_message_response(user_message)), "request_id": request_id})
        receipt = workspace_store.add_message(session.id, "assistant", message)
        await send_event(websocket, "message.created", session, {"message": _record_payload(_message_response(receipt)), "request_id": request_id})
        # message_id 让前端精确定位回执条目（在线与回放都不再靠"最近一条"猜测）。
        await send_event(
            websocket,
            "command.executed",
            session,
            {"command": content, "ok": ok, "message_id": receipt.id, "request_id": request_id},
        )

        # 状态联动：命令改了配置时，把变化同步给会话记录与顶栏（复用既有事件通道）。
        if ok and agent is not None:
            settings = getattr(agent, "settings", None)
            if cmd in ("/model", "/provider", "/config"):
                updated = workspace_store.update_session(
                    session.id,
                    active_provider=str(getattr(settings, "provider", "") or ""),
                    active_model=str(getattr(settings, "model", "") or ""),
                )
                if updated is not None:
                    session = updated
                    await send_event(websocket, "session.status", session, {"status": session.status, "request_id": request_id})
                # 命令可能改了 provider/model（进而改了窗口与输出上限）：同步给界面。
                await send_event(
                    websocket,
                    "context.updated",
                    session,
                    {**context_payload(session, agent), "request_id": request_id},
                )
            elif cmd == "/smartrouter":
                # 开关状态以 agent.settings 为准：全局 tier_service 读的是
                # ServerConfig.user_dir 的配置，而命令写的是 agent 级
                # ConfigManager（两处 user_dir 在生产环境也可能不同，见方案 §11）。
                # 同时清掉会话路由器的最近一次结果，避免快照回落到旧档位。
                router = session_routers.get(session.id)
                if router is not None:
                    router.last = None
                await send_event(
                    websocket,
                    "router.updated",
                    session,
                    {
                        "enabled": bool(getattr(settings, "smart_router_enabled", False)),
                        "tier": "",
                        "provider": "",
                        "model": "",
                        "request_id": request_id,
                    },
                )
            elif cmd in ("/save", "/memory"):
                # 长期记忆被改（/save 新增、/memory delete 删除）：推 memory.updated
                # 让侧栏 Memory 页签重拉条目列表（与上下文压缩的提示共用同一事件）。
                await send_event(
                    websocket,
                    "memory.updated",
                    session,
                    {"kind": "memory.command", "message": message.splitlines()[0] if message else "", "request_id": request_id},
                )
        return session

    @app.websocket("/api/ws/projects/{project_id}/sessions/{session_id}")
    async def session_socket(websocket: WebSocket, project_id: str, session_id: str) -> None:
        if not websocket_authorized(websocket):
            await websocket.accept()
            await websocket.send_json({"type": "error", "code": "not_authenticated", "message": "WebSocket 鉴权失败"})
            await websocket.close(code=4401)
            return
        if not websocket_origin_allowed(websocket):
            await websocket.accept()
            await websocket.send_json({"type": "error", "code": "origin_not_allowed", "message": "WebSocket 来源不被允许"})
            await websocket.close(code=4403)
            return
        try:
            project = require_project(project_id)
            session = require_session(session_id, project_id)
        except (ApiError, ProjectRegistryError):
            await websocket.accept()
            await websocket.send_json({"type": "error", "code": "session_not_found", "message": "项目或会话不存在"})
            await websocket.close(code=4404)
            return
        await websocket.accept()
        attach_connection(session.id, websocket)
        try:
            snapshot = {
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "root_path": project.root_path,
                },
                "session": _record_payload(_session_response(session)),
                # 当前模型的能力上限（窗口 / 输出上限 / 发送字段名）：界面据此显示
                # 使用率分母与实际下发的限制，不再依赖构建期常量。
                "context": context_payload(session),
                # 最近 500 条消息（正序）：段落落库后消息条数翻数倍，取最旧窗口
                # 会最先截掉最新消息（方案 07 §4.7）。
                "messages": [_record_payload(_message_response(item)) for item in workspace_store.list_recent_messages(session.id)],
                # 项目级长期记忆条目：重连即可见（此前是写死的空数组）。
                "memory": memory_snapshot(session, project),
                "safety": {
                    "project_id": project.id,
                    "hitl": "server-managed",
                    "status": "ready",
                },
                # 智能路由状态：开关 + 最近一次路由到的档位与模型（重连后仍可回显）
                "router": router_snapshot(session.id),
                "audit": {
                    "tool_calls": 0,
                    "tool_failures": 0,
                    "approvals": 0,
                },
                "last_sequence": 0,
            }
            # 卡片恢复（plans/enhancement 的重连方案）：
            # 1) replay = 历史里的卡片类事件，前端按序喂给同一套 reducer；
            # 2) pending = 内存桥里仍挂起的审批 / 计划审阅，恢复成"可继续应答"的卡片。
            # 回放窗口按类型取"最近 N 张卡片"：高频事件不再挤占窗口（message.delta 已
            # 改为只广播、不落库，Optimization 01 §5.1），长会话重连仍能拿到最新卡片
            # （方案 07 §4.6b）。
            snapshot["last_sequence"] = workspace_store.latest_event_sequence(session.id)
            replay_events = _replay_card_events(
                workspace_store.list_card_events(
                    session.id,
                    (*_REPLAY_EVENT_TYPES, _ROUTER_EVENT_TYPE),
                    limit=_REPLAY_CARD_LIMIT,
                )
            )
            approval_bridge = session_approvals.get(session.id)
            pending_payload = getattr(approval_bridge, "pending_payload", None)
            review_bridge = session_reviews.get(session.id)
            review_payload = getattr(review_bridge, "pending_payload", None)
            pending: dict[str, Any] = {}
            if pending_payload:
                pending["approval"] = pending_payload
            if review_payload:
                pending["plan_review"] = review_payload
            snapshot["replay"] = replay_events
            snapshot["pending"] = pending
            # 审计计数改用全量 SQL COUNT：不再受回放窗口影响（方案 07 §4.6b）。
            snapshot["audit"] = {
                "tool_calls": workspace_store.count_events(session.id, "tool.started"),
                "tool_failures": workspace_store.count_tool_failures(session.id),
                "approvals": (
                    workspace_store.count_events(session.id, "approval.requested")
                    + workspace_store.count_events(session.id, "approval.resolved")
                ),
            }
            # 只占一个序号，不落快照正文（Optimization 01 §5.3）：快照是瞬时状态，
            # 每次（重）连都会重新生成，而它的 replay 内容本身又是从 events 表读出来的
            # ——落库等于把同一批数据存两遍（此前 76 次连接占了 21.8 MB）。
            # 下面仍用完整的 snapshot 构造发给前端的载荷，所以 WS 收到的内容一字未变。
            snapshot_event = workspace_store.append_event(
                session.id, session.project_id, "session.snapshot", {}
            )
            snapshot["last_sequence"] = snapshot_event.sequence
            snapshot_event = EventRecord(
                snapshot_event.event_id, snapshot_event.sequence, snapshot_event.session_id,
                snapshot_event.project_id, snapshot_event.event_type, snapshot, snapshot_event.occurred_at,
            )
            await websocket.send_json(_event_payload(snapshot_event))
            heartbeat_misses = 0
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=resolved_config.ws_heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    heartbeat_misses += 1
                    if heartbeat_misses >= 2:
                        await websocket.close(code=4408)
                        break
                    await websocket.send_json({"type": "ping", "occurred_at": datetime.now().isoformat()})
                    continue
                except WebSocketDisconnect:
                    break
                heartbeat_misses = 0
                if len(raw.encode("utf-8")) > resolved_config.ws_max_message_bytes:
                    await send_event(websocket, "error", session, {"code": "message_too_large", "message": "WebSocket 消息超过大小限制"})
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await send_event(websocket, "error", session, {"code": "invalid_message", "message": "消息必须是有效 JSON"})
                    continue
                if not isinstance(payload, dict):
                    await send_event(websocket, "error", session, {"code": "invalid_message", "message": "消息必须是 JSON 对象"})
                    continue
                message_type = payload.get("type")
                request_id = str(payload.get("request_id", "")).strip()
                if len(request_id) > 200:
                    await send_event(websocket, "error", session, {"code": "invalid_request_id", "message": "request_id 过长"})
                    continue
                if message_type == "ping":
                    await websocket.send_json({"type": "pong", "request_id": request_id})
                    continue
                if message_type == "pong":
                    continue
                if message_type == "cancel":
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    session = await perform_cancel(websocket, session, request_id)
                    continue
                if message_type in {"approve", "reject", "ask_answer", "ask_cancel"}:
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    bridge = session_approvals.get(session.id)
                    if bridge is None or not bridge.has_pending:
                        await send_event(websocket, "error", session, {"code": "no_pending_approval", "message": "当前没有等待中的审批或提问", "request_id": request_id})
                        continue
                    approval_id = str(payload.get("approval_id", "")).strip()
                    if approval_id and approval_id != bridge.pending_id:
                        await send_event(websocket, "error", session, {"code": "approval_mismatch", "message": "approval_id 与当前待决项不符", "request_id": request_id})
                        continue
                    if message_type in {"ask_answer", "ask_cancel"}:
                        if message_type == "ask_cancel":
                            resolved = bridge.resolve_ask(approval_id, None)
                        else:
                            answers = payload.get("answers")
                            if not isinstance(answers, dict):
                                await send_event(websocket, "error", session, {"code": "invalid_answers", "message": "answers 必须是对象", "request_id": request_id})
                                continue
                            resolved = bridge.resolve_ask(
                                approval_id, {str(key): str(value) for key, value in answers.items()}
                            )
                    else:
                        decision = _approval_decision(message_type, payload)
                        # 「本会话放行」独立于决策本身：客户端可以同时改参并放行。
                        if message_type == "approve" and _wants_session_scope(payload):
                            agent = session_agents.get(session.id)
                            policy = getattr(agent, "approval_policy", None)
                            if policy is not None:
                                policy.allow_all()
                        resolved = bridge.resolve_approval(approval_id, decision)
                    if not resolved:
                        await send_event(websocket, "error", session, {"code": "approval_not_accepted", "message": "该审批已不再等待应答", "request_id": request_id})
                    continue
                if message_type == "team_resume":
                    # 团队卡上的「继续」（方案 15 §4.4）：走与 /team 相同的互斥与幂等规则。
                    task = running_tasks.get(session.id)
                    if task and not task.done():
                        await send_event(websocket, "error", session, {"code": "session_busy", "message": "会话正在运行", "request_id": request_id})
                        continue
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    updated = workspace_store.update_session(session.id, status="running")
                    if updated:
                        session = updated
                        await send_event(websocket, "session.status", session, {"status": "running", "request_id": request_id})
                    running_tasks[session.id] = asyncio.create_task(
                        run_team_resume_turn(websocket, project, session, payload, request_id)
                    )
                    continue
                if message_type == "plan_decision":
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    review = session_reviews.get(session.id)
                    if review is None or not review.has_pending:
                        await send_event(websocket, "error", session, {"code": "no_pending_review", "message": "当前没有等待中的计划审阅", "request_id": request_id})
                        continue
                    review_id = str(payload.get("review_id", "")).strip()
                    if review_id and review_id != review.pending_id:
                        await send_event(websocket, "error", session, {"code": "review_mismatch", "message": "review_id 与当前待决项不符", "request_id": request_id})
                        continue
                    action = str(payload.get("action", "")).strip().lower()
                    feedback = str(payload.get("feedback", "")).strip()
                    if not review.resolve(review_id, action, feedback):
                        await send_event(websocket, "error", session, {"code": "review_not_accepted", "message": "该审阅已不再等待应答，或 action 非法（execute / cancel / replan）", "request_id": request_id})
                    continue
                if message_type != "user_message":
                    await send_event(websocket, "error", session, {"code": "invalid_message_type", "message": "不支持的消息类型", "request_id": request_id})
                    continue
                content = str(payload.get("content", "")).strip()
                if not content or len(content) > 1_000_000:
                    await send_event(websocket, "error", session, {"code": "invalid_content", "message": "消息内容不能为空且不能超过限制", "request_id": request_id})
                    continue
                turn_kind, goal, options = _parse_task_command(content)
                if turn_kind in {"plan", "team"} and not goal:
                    await send_event(websocket, "error", session, {"code": "missing_goal", "message": f"用法: /{turn_kind} <任务描述>", "request_id": request_id})
                    continue
                if turn_kind == "team_resume":
                    # 续跑不再是命令行形态（方案 15 §2 非目标）：让用户在团队卡上点，
                    # 那里能勾范围；否则它会被当成新任务重跑一遍，白烧一遍预算。
                    await send_event(websocket, "error", session, {
                        "code": "unknown_command",
                        "message": "未知命令：/team resume（续跑请在团队卡上点「继续」，权限类失败可直接勾选允许修改的范围）",
                        "request_id": request_id,
                    })
                    continue
                # slash 命令通道（plans/enhancement/01-web-slash-commands.md）：
                # /cancel 别名等价于 cancel 消息（必须在运行中也可用）；其余命令
                # 与普通输入共用互斥规则，避免 /clear、/model 等与运行中轮次并发。
                if turn_kind == "chat" and content.startswith("/"):
                    cmd = content.split(maxsplit=1)[0].lower()
                    if cmd in ("/cancel", "/c"):
                        if not request_is_new(session.id, request_id):
                            await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                            continue
                        session = await perform_cancel(websocket, session, request_id)
                        continue
                    task = running_tasks.get(session.id)
                    if task is not None and not task.done():
                        await send_event(websocket, "error", session, {"code": "session_busy", "message": "会话正在运行，请先停止或取消当前任务", "request_id": request_id})
                        continue
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    session = await run_command_turn(websocket, project, session, content, request_id)
                    continue
                task = running_tasks.get(session.id)
                if task and not task.done():
                    await send_event(websocket, "error", session, {"code": "session_busy", "message": "会话正在运行", "request_id": request_id})
                    continue
                if not request_is_new(session.id, request_id):
                    await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                    continue
                user_message = workspace_store.add_message(session.id, "user", content)
                await send_event(websocket, "message.created", session, {"message": _record_payload(_message_response(user_message)), "request_id": request_id})
                updated = workspace_store.update_session(session.id, status="running")
                if updated:
                    session = updated
                    await send_event(websocket, "session.status", session, {"status": "running", "request_id": request_id})
                running_tasks[session.id] = asyncio.create_task(
                    _turn_coroutine(websocket, project, session, turn_kind, goal, options, content, request_id)
                )
        finally:
            detach_connection(session_id, websocket)
            task = running_tasks.get(session_id)
            if task is not None and task.done():
                running_tasks.pop(session_id, None)

    async def reject_socket(websocket: WebSocket, code: str, message: str, close_code: int) -> None:
        """接受连接后立刻用一条 error 说明原因并关闭。

        先 accept 再 send 是为了让客户端拿得到可读的错误码，而不是一个裸的
        握手失败。
        """
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": code, "message": message})
        await websocket.close(code=close_code)

    @app.websocket("/api/ws/projects/{project_id}/terminal")
    async def project_terminal(websocket: WebSocket, project_id: str) -> None:
        """项目 cwd 绑定的终端通道。

        独立于会话 socket：终端生命周期与 Agent 轮次正交（cancel 不该杀掉
        终端），且终端输出**不落库** —— 走会话 socket 的 append_event 会以每秒
        几十条的频率灌爆 SQLite 并污染 session.snapshot 的序号与重放。
        """
        if not websocket_authorized(websocket):
            await reject_socket(websocket, "not_authenticated", "WebSocket 鉴权失败", 4401)
            return
        if not websocket_origin_allowed(websocket):
            await reject_socket(websocket, "origin_not_allowed", "WebSocket 来源不被允许", 4403)
            return
        if not resolved_config.terminal_enabled:
            await reject_socket(websocket, "terminal_disabled", "终端通道已关闭", 4403)
            return
        if not resolved_config.ws_auth_token and not resolved_config.allowed_origins:
            # 默认情况下 Origin 校验是关的（allowed_origins 为空即一律放行），
            # 而浏览器发 WebSocket 不受 CORS 约束 —— 加了终端就等于给任意网页
            # 一个 shell。所以这里要求至少配上 token 或 Origin 白名单之一。
            logger.warning(
                "终端通道拒绝了未鉴权的连接：请配置 ROUTIVUS_SERVER_TOKEN "
                "或 ROUTIVUS_ALLOWED_ORIGINS 后再启用终端"
            )
            await reject_socket(
                websocket,
                "terminal_auth_required",
                "终端通道要求配置 ROUTIVUS_SERVER_TOKEN 或 ROUTIVUS_ALLOWED_ORIGINS",
                4403,
            )
            return
        try:
            project = require_project(project_id)
        except (ApiError, ProjectRegistryError):
            await reject_socket(websocket, "project_not_found", "项目不存在", 4404)
            return

        root = Path(project.root_path).resolve()
        await websocket.accept()
        session: TerminalSession | None = None
        seen_requests: set[str] = set()
        heartbeat_misses = 0

        def is_new_request(request_id: str) -> bool:
            if not request_id:
                return True
            if request_id in seen_requests:
                return False
            if len(seen_requests) > 1000:
                seen_requests.clear()
            seen_requests.add(request_id)
            return True

        try:
            while True:
                if (
                    session is not None
                    and time.monotonic() - session.last_activity > resolved_config.terminal_idle_timeout
                ):
                    await session.close("idle_timeout")
                    break
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=resolved_config.ws_heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    heartbeat_misses += 1
                    if heartbeat_misses >= 2:
                        if session is not None:
                            await session.close("idle_timeout")
                        else:
                            await websocket.close(code=4408)
                        break
                    await websocket.send_json({"type": "ping", "occurred_at": datetime.now().isoformat()})
                    continue
                except WebSocketDisconnect:
                    break
                heartbeat_misses = 0
                if len(raw.encode("utf-8")) > resolved_config.ws_max_message_bytes:
                    await websocket.send_json({"type": "error", "code": "message_too_large", "message": "消息超过大小限制"})
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "code": "invalid_message", "message": "消息必须是有效 JSON"})
                    continue
                if not isinstance(payload, dict):
                    await websocket.send_json({"type": "error", "code": "invalid_message", "message": "消息必须是 JSON 对象"})
                    continue
                message_type = payload.get("type")
                request_id = str(payload.get("request_id", "")).strip()
                if message_type == "ping":
                    await websocket.send_json({"type": "pong", "request_id": request_id})
                    continue
                if message_type == "pong":
                    continue

                if message_type == "terminal.open":
                    if session is not None:
                        await websocket.send_json({"type": "error", "code": "terminal_already_open", "message": "该连接已有终端", "request_id": request_id})
                        continue
                    if not is_new_request(request_id):
                        await websocket.send_json({"type": "error", "code": "duplicate_request", "request_id": request_id})
                        continue
                    total = len(terminals)
                    per_project = sum(1 for item in terminals.values() if item.spec.project_id == project_id)
                    if (
                        total >= resolved_config.terminal_max_sessions
                        or per_project >= resolved_config.terminal_max_per_project
                    ):
                        await websocket.send_json({"type": "error", "code": "terminal_limit_reached", "message": "终端连接数已达上限"})
                        await websocket.close(code=4429)
                        break
                    terminal_id = new_terminal_id()
                    cols = _clamp_int(payload.get("cols"), resolved_config.terminal_cols, 20, 400)
                    rows = _clamp_int(payload.get("rows"), resolved_config.terminal_rows, 5, 200)
                    # cwd 只来自服务端解析过的项目根；请求体里的任何路径字段
                    # 都不参与构造 spec（见 TerminalSpec）。
                    spec = TerminalSpec(
                        terminal_id=terminal_id,
                        project_id=project.id,
                        cwd=root,
                        shell=default_shell(resolved_config.terminal_shell),
                        cols=cols,
                        rows=rows,
                        env=build_env(root),
                    )
                    try:
                        backend = app.state.terminal_factory(spec)
                    except TerminalUnavailableError as exc:
                        await websocket.send_json({"type": "error", "code": "terminal_unavailable", "message": str(exc)})
                        await websocket.close(code=4500)
                        break

                    async def send_terminal(payload: dict[str, Any]) -> None:
                        await websocket.send_json(payload)

                    terminal = TerminalSession(
                        spec=spec,
                        backend=backend,
                        send=send_terminal,
                        audit=AuditLogger(root / ".routivus" / "audit.log", session_id=terminal_id),
                        chunk_bytes=resolved_config.terminal_chunk_bytes,
                        flush_interval=resolved_config.terminal_flush_interval,
                        queue_max=resolved_config.terminal_queue_max,
                        max_output_bytes=resolved_config.terminal_max_output_bytes,
                        kill_grace=resolved_config.terminal_kill_grace,
                        on_closed=lambda tid: terminals.pop(tid, None),
                    )
                    # 先登记再 await start()，中间没有挂起点 → 并发 open 不会双双越过上限。
                    terminals[terminal_id] = terminal
                    try:
                        await terminal.start()
                    except Exception:
                        terminals.pop(terminal_id, None)
                        logger.exception("terminal start failed project_id=%s", project_id)
                        await websocket.send_json({"type": "error", "code": "terminal_start_failed", "message": "终端启动失败"})
                        await websocket.close(code=4500)
                        break
                    session = terminal
                    await websocket.send_json(
                        {
                            "type": "terminal.opened",
                            "terminal_id": terminal_id,
                            "cwd": str(root),
                            "shell": spec.shell,
                            "backend": backend.name,
                            "cols": cols,
                            "rows": rows,
                            "request_id": request_id,
                        }
                    )
                    continue

                if session is None:
                    await websocket.send_json({"type": "error", "code": "terminal_not_opened", "message": "请先发送 terminal.open", "request_id": request_id})
                    continue

                if message_type == "terminal.input":
                    if not is_new_request(request_id):
                        await websocket.send_json({"type": "error", "code": "duplicate_request", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                        continue
                    data = payload.get("data")
                    if not isinstance(data, str):
                        await websocket.send_json({"type": "error", "code": "invalid_message", "message": "data 必须是字符串", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                        continue
                    if len(data.encode("utf-8")) > resolved_config.terminal_max_input_bytes:
                        await websocket.send_json({"type": "error", "code": "input_too_large", "message": "输入超过大小限制", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                        continue
                    forwarded, reason = await session.write(data)
                    if not forwarded:
                        if reason != "terminal_closed":
                            await websocket.send_json({"type": "error", "code": reason, "message": "命令被策略层拒绝", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                        continue
                    await websocket.send_json({"type": "terminal.input.ack", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                    continue

                if message_type == "terminal.resize":
                    cols, rows = await session.resize(
                        _clamp_int(payload.get("cols"), resolved_config.terminal_cols, 20, 400),
                        _clamp_int(payload.get("rows"), resolved_config.terminal_rows, 5, 200),
                    )
                    await websocket.send_json({"type": "terminal.resized", "terminal_id": session.spec.terminal_id, "cols": cols, "rows": rows})
                    continue

                if message_type == "terminal.clear":
                    # ConPTY 下清不掉远端屏幕缓冲；这是纯客户端操作，服务端只回
                    # ack 做协议对称 —— 绝不注入 cls 命令（那是通过协议字段做命令注入）。
                    await websocket.send_json({"type": "terminal.cleared", "terminal_id": session.spec.terminal_id})
                    continue

                if message_type == "terminal.close":
                    if not is_new_request(request_id):
                        await websocket.send_json({"type": "error", "code": "duplicate_request", "terminal_id": session.spec.terminal_id, "request_id": request_id})
                        continue
                    await session.close("client_closed")
                    session = None
                    continue

                await websocket.send_json({"type": "error", "code": "invalid_message_type", "message": "不支持的终端消息类型", "request_id": request_id})
        finally:
            if session is not None:
                await session.close("client_closed")

    app.include_router(router)

    # 桌面模式：同源托管前端构建产物，窗口与 API 因此同源，
    # 不再需要 CORS / 反向代理 / Origin 白名单。
    # 前端用的是 hash 路由（#/notes 这类），路径不会发到服务端，
    # 所以 StaticFiles(html=True) 就够，不需要 SPA history fallback。
    # 挂载点放在最后：/healthz 与 /api/* 先注册，优先匹配。
    if resolved_config.static_dir is not None:
        static_dir = resolved_config.static_dir
        if static_dir.is_dir() and (static_dir / "index.html").is_file():
            assets = static_dir / "assets"
            if assets.is_dir():
                app.mount("/assets", StaticFiles(directory=assets), name="assets")
            app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
            logger.info("已启用同源托管前端产物：%s", static_dir)
        else:
            logger.warning(
                "ROUTIVUS_STATIC_DIR 下没有可用的前端产物（需要 index.html）：%s", static_dir
            )

    return app
