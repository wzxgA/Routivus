"""FastAPI application for the Routivus Web Console."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import tempfile
import time
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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from routivus import __version__
from routivus.server.config import ServerConfig
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


def create_app(
    *,
    registry: ProjectRegistry | None = None,
    config: ServerConfig | None = None,
    store: WorkspaceStore | None = None,
    agent_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    """Create an isolated application instance for production or tests."""

    resolved_config = config or ServerConfig.from_env()
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
    app = FastAPI(title="Routivus Web Console", version=__version__)
    app.state.project_registry = project_registry
    app.state.server_config = resolved_config
    app.state.workspace_store = workspace_store
    app.state.agent_factory = agent_factory or _build_default_agent
    app.state.ws_connections = {}
    app.state.session_agents = {}
    app.state.running_tasks = {}
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

    running_tasks: dict[str, Any] = app.state.running_tasks
    session_agents: dict[str, Any] = app.state.session_agents
    connections: dict[str, list[WebSocket]] = app.state.ws_connections

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

    async def run_agent_turn(websocket: WebSocket | None, project: Any, session: SessionRecord, content: str, request_id: str = "") -> None:
        assistant_parts: list[str] = []
        failed = False
        tool_started: dict[str, float] = {}
        tool_calls = 0
        tool_failures = 0

        def apply_usage(current: SessionRecord, usage: Any) -> SessionRecord:
            updated = workspace_store.update_session(
                current.id,
                prompt_tokens=current.prompt_tokens + max(0, int(getattr(usage, "prompt_tokens", 0))),
                completion_tokens=current.completion_tokens + max(0, int(getattr(usage, "completion_tokens", 0))),
                total_tokens=current.total_tokens + max(0, int(getattr(usage, "total_tokens", 0))),
            )
            return updated or current

        try:
            factory = app.state.agent_factory
            if factory is None:
                await send_event(websocket, "error", session, {"code": "agent_unavailable", "message": "Agent 尚未配置", "request_id": request_id})
                updated = workspace_store.update_session(session.id, status="failed")
                if updated:
                    await send_event(websocket, "session.status", updated, {"status": "failed"})
                return
            agent = session_agents.get(session.id)
            if agent is None:
                agent = factory(project, session)
                if inspect.isawaitable(agent):
                    agent = await agent
                session_agents[session.id] = agent
            stream = agent.run(content)
            if inspect.isawaitable(stream):
                stream = await stream
            async for item in stream:
                kind = getattr(item, "kind", "")
                text = str(getattr(item, "text", "") or "")
                if kind in {"content", "thinking"} and text:
                    if kind == "content":
                        assistant_parts.append(text)
                    await send_event(websocket, "message.delta", session, {"kind": kind, "text": text, "request_id": request_id})
                elif kind == "tool_call":
                    call = getattr(item, "tool_call", None)
                    call_id = str(getattr(call, "id", ""))
                    tool_started[call_id] = time.perf_counter()
                    tool_calls += 1
                    data = {
                        "tool_call_id": call_id,
                        "name": getattr(call, "name", ""),
                        "arguments": getattr(call, "arguments", ""),
                        "request_id": request_id,
                    }
                    await send_event(websocket, "tool.started", session, data)
                elif kind == "tool_result":
                    result = getattr(item, "tool_result", None)
                    result_id = str(getattr(result, "tool_call_id", ""))
                    ok = bool(getattr(result, "ok", False))
                    if not ok:
                        tool_failures += 1
                    data = {
                        "tool_call_id": result_id,
                        "name": getattr(result, "name", ""),
                        "ok": ok,
                        "output": getattr(result, "output", ""),
                        "error": getattr(result, "error", ""),
                        "duration_ms": round((time.perf_counter() - tool_started.pop(result_id, time.perf_counter())) * 1000),
                        "request_id": request_id,
                    }
                    workspace_store.add_message(session.id, "tool", data.get("output") or data.get("error") or "", tool_name=data["name"], tool_result=data.get("output") or data.get("error"))
                    await send_event(websocket, "tool.completed", session, data)
                    await send_event(websocket, "audit.updated", session, {
                        "tool_calls": tool_calls,
                        "tool_failures": tool_failures,
                        "request_id": request_id,
                    })
                elif kind == "approval":
                    decision = getattr(item, "decision", None)
                    await send_event(websocket, "approval.resolved", session, {
                        "tool_call_id": getattr(getattr(item, "tool_call", None), "id", ""),
                        "decision": "approve" if getattr(decision, "allow", False) else "reject",
                        "reason": getattr(decision, "reason", ""),
                        "request_id": request_id,
                    })
                elif kind == "ask_user":
                    await send_event(websocket, "approval.requested", session, {
                        "request_id": request_id,
                        "ask": _record_payload(getattr(item, "ask", None)),
                    })
                elif kind in {"usage", "context_usage"}:
                    usage = getattr(item, "usage", None)
                    if usage:
                        session = apply_usage(session, usage)
                        await send_event(websocket, "session.usage", session, {**_record_payload(usage), "request_id": request_id})
                elif kind in {"context_warning", "context_compacted", "context_overflow", "budget_exceeded"}:
                    await send_event(websocket, "memory.updated", session, {
                        "kind": kind,
                        "message": text,
                        "request_id": request_id,
                    })
                elif getattr(item, "plan", None) is not None or kind.startswith("plan_"):
                    await send_event(websocket, "plan.updated", session, {
                        "kind": kind,
                        "payload": _record_payload(item),
                        "request_id": request_id,
                    })
                elif getattr(item, "team_id", "") or kind.startswith("team_") or kind.startswith("task_") or kind in {"batch_started", "subtask_started", "subtask_done", "subtask_failed"}:
                    await send_event(websocket, "team.updated", session, {
                        "kind": kind,
                        "payload": _record_payload(item),
                        "request_id": request_id,
                    })
                elif kind == "error":
                    failed = True
                    await send_event(websocket, "error", session, {"code": "agent_error", "message": text, "request_id": request_id})
                elif kind == "done":
                    usage = getattr(item, "usage", None)
                    if usage:
                        session = apply_usage(session, usage)
                        await send_event(websocket, "session.usage", session, {**_record_payload(usage), "request_id": request_id})
            if assistant_parts:
                message = workspace_store.add_message(session.id, "assistant", "".join(assistant_parts))
                await send_event(websocket, "message.completed", session, {"message": _record_payload(_message_response(message)), "request_id": request_id})
            updated = workspace_store.update_session(session.id, status="failed" if failed else "completed")
            if updated:
                await send_event(websocket, "session.status", updated, {"status": updated.status, "request_id": request_id})
        except asyncio.CancelledError:
            updated = workspace_store.update_session(session.id, status="cancelled")
            if updated:
                try:
                    await send_event(websocket, "session.status", updated, {"status": "cancelled", "request_id": request_id})
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.exception("agent turn failed session_id=%s", session.id)
            updated = workspace_store.update_session(session.id, status="failed")
            try:
                await send_event(websocket, "error", session, {"code": "agent_error", "message": str(exc), "request_id": request_id})
                if updated:
                    await send_event(websocket, "session.status", updated, {"status": "failed", "request_id": request_id})
            except Exception:
                pass

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
                    task = running_tasks.get(session.id)
                    if task and not task.done():
                        task.cancel()
                    else:
                        updated = workspace_store.update_session(session.id, status="cancelled")
                        if updated:
                            session = updated
                            await send_event(websocket, "session.status", session, {"status": "cancelled", "request_id": request_id})
                    continue
                if message_type in {"approve", "reject"}:
                    if not request_is_new(session.id, request_id):
                        await send_event(websocket, "error", session, {"code": "duplicate_request", "request_id": request_id})
                        continue
                    await send_event(websocket, "approval.resolved", session, {
                        "request_id": request_id,
                        "decision": payload.get("decision", "approve" if message_type == "approve" else "reject"),
                    })
                    continue
                if message_type != "user_message":
                    await send_event(websocket, "error", session, {"code": "invalid_message_type", "message": "不支持的消息类型", "request_id": request_id})
                    continue
                content = str(payload.get("content", "")).strip()
                if not content or len(content) > 1_000_000:
                    await send_event(websocket, "error", session, {"code": "invalid_content", "message": "消息内容不能为空且不能超过限制", "request_id": request_id})
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
                running_tasks[session.id] = asyncio.create_task(run_agent_turn(websocket, project, session, content, request_id))
        finally:
            detach_connection(session_id, websocket)
            task = running_tasks.get(session_id)
            if task is not None and task.done():
                running_tasks.pop(session_id, None)

    app.include_router(router)
    return app
