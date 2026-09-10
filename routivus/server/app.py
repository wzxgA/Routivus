"""FastAPI application for the Routivus Web Console."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shlex
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
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
from routivus.safety.audit import AuditLogger
from routivus.safety.hitl import ApprovalDecision
from routivus.server.approval import ApprovalBridge
from routivus.server.config import ServerConfig
from routivus.server.logging_setup import install_token_redaction
from routivus.server.plan_review import PlanReviewBridge
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
    ModelRequest,
    ProviderCreateRequest,
    ProviderKeyRequest,
    ProviderUpdateRequest,
    ProviderView,
    RootGrantRequest,
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


def _task_card_view(task: Any, *, mode: str) -> dict[str, Any]:
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
    return view


def _plan_card_view(plan: Any, *, mode: str) -> dict[str, Any]:
    """整份计划（Plan 与 TeamPlan 共用 goal / tasks / batches 三元组）。"""
    return {
        "goal": str(getattr(plan, "goal", "")),
        "tasks": [_task_card_view(task, mode=mode) for task in getattr(plan, "tasks", None) or []],
        "batches": [
            [str(task_id) for task_id in batch] for batch in getattr(plan, "batches", None) or []
        ],
    }


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
        data["task"] = _task_card_view(task, mode=mode)
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
        # needs_input / repair_scope_required 的原因分类：前端据此解释为何需要补写入范围。
        category = str(getattr(item, "failure_category", "") or "")
        if category:
            data["failure_category"] = category
    return data


def _parse_task_command(content: str) -> tuple[str, str, dict[str, Any]]:
    """把用户输入解析为 (turn_kind, goal, options)。

    前缀约定与 TUI 保持一致（`routivus/tui/controller.py:358-367`）：
    `/plan <任务>`、`/team <任务>`、`/team resume ...`，其余一律普通对话。
    返回的 turn_kind ∈ {"chat", "plan", "team", "team_resume"}。
    """
    text = content.strip()
    lowered = text.lower()
    if lowered.startswith("/plan"):
        return "plan", text[len("/plan") :].strip(), {}
    if lowered.startswith("/team"):
        rest = text[len("/team") :].strip()
        if rest.lower() == "resume" or rest.lower().startswith("resume "):
            return "team_resume", rest, _parse_resume_options(rest)
        return "team", rest, {}
    return "chat", text, {}


def _parse_resume_options(rest: str) -> dict[str, Any]:
    """解析 `/team resume [task_id] [--write-scope <路径>]...`。

    用法错误一律以 `error` 字段返回（调用方回一条可读错误），不猜测用户意图：
    写入范围必须显式声明，这是 fail closed 的前提。
    """
    try:
        parts = shlex.split(rest)[1:]  # 丢掉 "resume"
    except ValueError as exc:
        return {"error": f"命令解析失败: {exc}"}
    task_id = ""
    claims: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token == "--write-scope":
            if index + 1 >= len(parts):
                return {"error": "用法: /team resume [task_id] --write-scope <路径>"}
            claims.append(parts[index + 1])
            index += 2
            continue
        if token.startswith("--"):
            return {"error": f"未知选项: {token}（用法: /team resume [task_id] --write-scope <路径>）"}
        if task_id:
            return {"error": "只能指定一个 task_id（用法: /team resume [task_id] --write-scope <路径>）"}
        task_id = token
        index += 1
    return {"task_id": task_id, "claims": claims}


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


def _build_default_agent(project: Any, session: SessionRecord) -> Any:
    """Build the existing ReAct stack for a project-scoped WebSocket turn."""
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
        retry_enabled=settings.llm_retry_enabled,
        max_retries=settings.llm_max_retries,
        retry_base_delay=settings.llm_retry_base_delay,
        retry_max_delay=settings.llm_retry_max_delay,
        retry_jitter=settings.llm_retry_jitter,
        retry_total_timeout=settings.llm_retry_total_timeout,
        respect_retry_after=settings.llm_respect_retry_after,
    )
    audit = AuditLogger(root / ".routivus" / "audit.log", session_id=session.id)
    tools = build_registry(
        base_dir=root,
        max_output_chars=settings.max_tool_output_chars,
        guard=lambda name, args: guard_tool_call(root, name, args),
        audit=audit,
        ask_user_enabled=settings.ask_user_enabled,
    )
    memory = MemoryManager(
        root,
        project_memory_max_chars=settings.project_memory_max_chars,
        memory_prompt_max_chars=settings.memory_prompt_max_chars,
    )
    return ReActAgent(
        llm=llm,
        tools=tools,
        settings=settings,
        approval_policy=HITLPolicy(enabled=settings.hitl),
        audit=audit,
        memory_manager=memory,
    )


def _record_payload(record: Any) -> dict[str, Any]:
    return jsonable_encoder(record.model_dump(mode="json") if hasattr(record, "model_dump") else record)


@asynccontextmanager
async def _terminal_lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """服务退出时收割所有终端。

    没有这一步，Ctrl-C 停服务会留下孤儿的 cmd.exe 及其子进程 —— 正是 Phase 5
    完成标准后半句要求覆盖的场景。
    """
    yield
    for terminal in list(getattr(app.state, "terminals", {}).values()):
        try:
            await terminal.close("server_shutdown")
        except Exception:  # pragma: no cover - 退出路径尽力而为
            logger.debug("terminal shutdown failed terminal_id=%s", terminal.spec.terminal_id, exc_info=True)


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
    app.state.agent_factory = agent_factory or _build_default_agent
    app.state.ws_connections = {}
    app.state.session_agents = {}
    app.state.running_tasks = {}
    app.state.session_approvals = {}
    # 计划 / 团队审阅桥接，以及会话内最近一个可续跑的计划执行器（供 /team resume）。
    app.state.session_reviews = {}
    app.state.session_resumable = {}
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
        return [ActivityResponse(**item) for item in workspace_store.activity(from_date, to_date)]

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
        require_session(session_id)
        workspace_store.delete_session(session_id)
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
        try:
            active = config_manager.active()
            active_provider = active.provider_name
            active_model = active.model
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
        )

    def _ensure_ok(result: Any) -> None:
        if not getattr(result, "ok", False):
            raise ApiError(422, "config_rejected", str(getattr(result, "message", "配置被拒绝")))

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
        return _snapshot()

    @router.put("/config/tiers/{tier_name}", response_model=ConfigSnapshot)
    async def set_tier(tier_name: str, payload: TierRequest) -> ConfigSnapshot:
        _ensure_ok(tier_service.set_tier(tier_name, payload.provider, payload.model))
        return _snapshot()

    @router.delete("/config/tiers/{tier_name}", response_model=ConfigSnapshot)
    async def clear_tier(tier_name: str) -> ConfigSnapshot:
        _ensure_ok(tier_service.clear_tier(tier_name))
        return _snapshot()

    @router.post("/config/smart-router", response_model=ConfigSnapshot)
    async def set_smart_router(payload: SmartRouterRequest) -> ConfigSnapshot:
        _ensure_ok(tier_service.set_enabled(payload.enabled))
        return _snapshot()

    # ---------- 桌面端专用：运行时白名单授权 ----------

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
    session_resumable: dict[str, tuple[str, Any]] = app.state.session_resumable
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

    async def send_event(websocket: WebSocket | None, event_type: str, session: SessionRecord, data: dict[str, Any] | None = None) -> None:
        payload = await emit(event_type, session, data)
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

    class _TurnForwarder:
        """一轮执行的共享转发器：把 agent / 计划 / 团队事件映射为会话事件。

        内容增量、工具生命周期、用量、错误这几类映射对 ReAct 轮与计划 / 团队的
        子任务同样适用，所以三者共用一份映射，避免三处各自漂移。
        """

        def __init__(self, websocket: WebSocket | None, session: SessionRecord, request_id: str) -> None:
            self.websocket = websocket
            self.session = session
            self.request_id = request_id
            self.assistant_parts: list[str] = []
            self.failed = False
            self.tool_started: dict[str, float] = {}
            self.tool_calls = 0
            self.tool_failures = 0

        async def emit(self, event_type: str, data: dict[str, Any]) -> None:
            await send_event(self.websocket, event_type, self.session, {**data, "request_id": self.request_id})

        def apply_usage(self, usage: Any) -> None:
            updated = workspace_store.update_session(
                self.session.id,
                prompt_tokens=self.session.prompt_tokens + max(0, int(getattr(usage, "prompt_tokens", 0))),
                completion_tokens=self.session.completion_tokens + max(0, int(getattr(usage, "completion_tokens", 0))),
                total_tokens=self.session.total_tokens + max(0, int(getattr(usage, "total_tokens", 0))),
            )
            if updated is not None:
                self.session = updated

        async def forward(self, item: Any) -> None:
            """把一个 AgentEvent 映射为会话事件。"""
            kind = getattr(item, "kind", "")
            text = str(getattr(item, "text", "") or "")
            if kind in {"content", "thinking"} and text:
                if kind == "content":
                    self.assistant_parts.append(text)
                await self.emit("message.delta", {"kind": kind, "text": text})
            elif kind == "tool_call":
                call = getattr(item, "tool_call", None)
                call_id = str(getattr(call, "id", ""))
                self.tool_started[call_id] = time.perf_counter()
                self.tool_calls += 1
                await self.emit("tool.started", {
                    "tool_call_id": call_id,
                    "name": getattr(call, "name", ""),
                    "arguments": getattr(call, "arguments", ""),
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
            elif kind == "approval":
                decision = getattr(item, "decision", None)
                await self.emit("approval.resolved", {
                    "tool_call_id": getattr(getattr(item, "tool_call", None), "id", ""),
                    "decision": "approve" if getattr(decision, "allow", False) else "reject",
                    "reason": getattr(decision, "reason", ""),
                })
            elif kind == "ask_user":
                await self.emit("approval.requested", {
                    "ask": _record_payload(getattr(item, "ask", None)),
                })
            elif kind in {"usage", "context_usage"}:
                usage = getattr(item, "usage", None)
                if usage:
                    self.apply_usage(usage)
                    await self.emit("session.usage", _record_payload(usage))
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
                    await self.emit("session.usage", _record_payload(usage))

        async def finish(self) -> None:
            """一轮正常结束：落库 assistant 正文并收敛会话状态。"""
            if self.assistant_parts:
                message = workspace_store.add_message(self.session.id, "assistant", "".join(self.assistant_parts))
                await self.emit("message.completed", {"message": _record_payload(_message_response(message))})
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
            await forwarder.forward(inner)
        usage = getattr(item, "usage", None)
        if usage is not None:
            forwarder.apply_usage(usage)
            await forwarder.emit("session.usage", _record_payload(usage))

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

    def _first_needs_input_task(executor: Any) -> str:
        plan = getattr(executor, "_last_plan", None)
        for task in getattr(plan, "tasks", None) or []:
            if str(getattr(task, "status", "")) == "needs_input":
                return str(getattr(task, "id", ""))
        return ""

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
            stream = agent.run(content)
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
            review = plan_review_bridge(session.id)
            review.mode = "plan"

            from routivus.agent.plan import PlanExecutor

            executor = PlanExecutor(**_executor_kwargs(agent), reviewer=review.review)
            session_resumable[session.id] = ("plan", executor)
            async for event in executor.run(goal):
                await forward_task_event(event, forwarder, mode="plan")
            await forwarder.finish()
        except asyncio.CancelledError:
            await handle_turn_cancelled(websocket, session, request_id)
            raise
        except Exception as exc:
            await handle_turn_failure(websocket, session, request_id, exc, label="plan turn")

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
            review = plan_review_bridge(session.id)
            review.mode = "team"

            from routivus.agent.team import TeamExecutor

            executor = TeamExecutor(**_executor_kwargs(agent), reviewer=review.review, project_root=Path(project.root_path))
            session_resumable[session.id] = ("team", executor)
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
        options: dict[str, Any],
        request_id: str = "",
    ) -> None:
        """`/team resume [task_id] [--write-scope <路径>]...`：Team 断点续跑。

        写入范围必须显式声明（由执行器再次校验），这是 fail closed 的前提。
        """
        forwarder = _TurnForwarder(websocket, session, request_id)
        stored = session_resumable.get(session.id)
        if stored is None or stored[0] != "team":
            await forwarder.close_with(
                "idle",
                error="没有可恢复的 Team 计划（需要先在本会话跑过一次 /team）",
                code="no_resumable_team",
            )
            return
        parse_error = str(options.get("error", "") or "")
        if parse_error:
            await forwarder.close_with("idle", error=parse_error, code="invalid_resume")
            return
        executor = stored[1]
        try:
            agent = session_agents.get(session.id)
            if agent is not None:
                bind_interactions(agent, session.id)

            from routivus.agent.team import ResourceClaim

            task_id = str(options.get("task_id", "") or "")
            claims = [ResourceClaim(pattern, "write") for pattern in options.get("claims", []) or []]
            if claims:
                target = task_id or _first_needs_input_task(executor)
                if not target:
                    await forwarder.close_with(
                        "idle",
                        error="没有处于 needs_input 的任务可恢复；如需续跑失败任务请直接发送 /team resume",
                        code="no_pending_task",
                    )
                    return
                stream = executor.resume_task_with_repair_scope(target, claims)
            else:
                stream = executor.resume(task_id)
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
        """按输入前缀选择回合执行器：`/plan`、`/team`、`/team resume` 或普通对话。"""
        if turn_kind == "plan":
            return run_plan_turn(websocket, project, session, goal, request_id)
        if turn_kind == "team":
            return run_team_turn(websocket, project, session, goal, request_id)
        if turn_kind == "team_resume":
            return run_team_resume_turn(websocket, project, session, options, request_id)
        return run_agent_turn(websocket, project, session, content, request_id)

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
                "messages": [_record_payload(_message_response(item)) for item in workspace_store.list_messages(session.id)],
                "memory": {
                    "project_id": project.id,
                    "scope": "project",
                    "status": "available",
                    "items": [],
                },
                "safety": {
                    "project_id": project.id,
                    "hitl": "server-managed",
                    "status": "ready",
                },
                "audit": {
                    "tool_calls": 0,
                    "tool_failures": 0,
                    "approvals": 0,
                },
                "last_sequence": 0,
            }
            prior_events = workspace_store.list_events(session.id, after_sequence=0, limit=1000)
            snapshot["last_sequence"] = prior_events[-1].sequence if prior_events else 0
            snapshot["audit"] = {
                "tool_calls": sum(item.event_type == "tool.started" for item in prior_events),
                "tool_failures": sum(item.event_type == "tool.completed" and not bool(item.data.get("ok", False)) for item in prior_events),
                "approvals": sum(item.event_type in {"approval.requested", "approval.resolved"} for item in prior_events),
            }
            snapshot_event = workspace_store.append_event(session.id, session.project_id, "session.snapshot", snapshot)
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
                    _cancel_bridges(session.id)
                    task = running_tasks.get(session.id)
                    if task and not task.done():
                        task.cancel()
                    else:
                        updated = workspace_store.update_session(session.id, status="cancelled")
                        if updated:
                            session = updated
                            await send_event(websocket, "session.status", session, {"status": "cancelled", "request_id": request_id})
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
                if turn_kind == "team_resume" and options.get("error"):
                    await send_event(websocket, "error", session, {"code": "invalid_resume", "message": str(options["error"]), "request_id": request_id})
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
