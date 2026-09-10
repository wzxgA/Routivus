"""FastAPI application for Phase 1 of the Routivus Web Console."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from routivus import __version__
from routivus.server.config import ServerConfig
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


def _to_response(record) -> ProjectResponse:  # type: ignore[no-untyped-def]
    return ProjectResponse(
        id=record.id,
        name=record.name,
        root_path=record.root_path,
        branch=record.branch,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        stats=ProjectStats(),
    )


def create_app(
    *,
    registry: ProjectRegistry | None = None,
    config: ServerConfig | None = None,
) -> FastAPI:
    """Create an isolated application instance for production or tests."""

    resolved_config = config or ServerConfig.from_env()
    project_registry = registry or ProjectRegistry(
        resolved_config.projects_file,
        resolved_config.workspace_roots,
    )
    app = FastAPI(title="Routivus Web Console", version=__version__)
    app.state.project_registry = project_registry
    app.state.server_config = resolved_config
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
        return [_to_response(record) for record in project_registry.list()]

    @router.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(payload: ProjectCreateRequest) -> ProjectResponse:
        return _to_response(project_registry.create(payload.name, payload.root_path))

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    async def get_project(project_id: str) -> ProjectResponse:
        return _to_response(project_registry.get(project_id))

    @router.patch("/projects/{project_id}", response_model=ProjectResponse)
    async def update_project(project_id: str, payload: ProjectUpdateRequest) -> ProjectResponse:
        return _to_response(
            project_registry.update(
                project_id,
                name=payload.name,
                root_path=payload.root_path,
            )
        )

    @router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_project(project_id: str) -> Response:
        project_registry.delete(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)
    return app
