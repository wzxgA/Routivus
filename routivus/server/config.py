"""Configuration for the local Routivus Web Console server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_paths(raw: str) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())


def _float_env(values: dict[str, str], key: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(values.get(key, str(default))))
    except (TypeError, ValueError):
        return default


def _int_env(values: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(values.get(key, str(default)))))
    except (TypeError, ValueError):
        return default


def _bool_env(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() not in ("off", "0", "false")


@dataclass(frozen=True)
class ServerConfig:
    """Resolved server settings.

    The default workspace is the directory from which the server is started.
    A deployment can explicitly provide ``ROUTIVUS_WORKSPACE_ROOTS`` using the
    platform path separator (``;`` on Windows, ``:`` on POSIX).
    """

    projects_file: Path
    workspace_roots: tuple[Path, ...]
    database_path: Path = Path("~/.routivus/workspace.sqlite3")
    host: str = "127.0.0.1"
    port: int = 18_765
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    allowed_origins: tuple[str, ...] = ()
    ws_auth_token: str = ""
    ws_heartbeat_interval: float = 30.0
    ws_max_message_bytes: int = 1_048_576
    approval_timeout: float = 300.0
    terminal_enabled: bool = True
    terminal_backend: str = "auto"
    terminal_shell: str = ""
    terminal_max_sessions: int = 4
    terminal_max_per_project: int = 2
    terminal_idle_timeout: float = 900.0
    terminal_command_timeout: float = 120.0
    terminal_max_output_bytes: int = 262_144
    terminal_max_input_bytes: int = 65_536
    terminal_chunk_bytes: int = 8_192
    terminal_flush_interval: float = 0.033
    terminal_queue_max: int = 256
    terminal_kill_grace: float = 3.0
    terminal_cols: int = 120
    terminal_rows: int = 30
    # 桌面模式：由 FastAPI 直接托管前端构建产物（frontend/dist），
    # 使窗口与 API 同源，省掉 CORS / 反向代理 / Origin 白名单。
    static_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        """Compatibility alias for integrations that call it ``db_path``."""
        return self.database_path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServerConfig":
        values = env if env is not None else os.environ
        user_dir = Path(values.get("ROUTIVUS_USER_DIR", "~/.routivus")).expanduser()
        projects_file = Path(
            values.get("ROUTIVUS_PROJECTS_FILE", str(user_dir / "projects.json"))
        ).expanduser()
        database_path = Path(
            values.get("ROUTIVUS_DATABASE_PATH", values.get("ROUTIVUS_WORKSPACE_DB", values.get("ROUTIVUS_DB_PATH", str(user_dir / "workspace.sqlite3"))))
        ).expanduser()

        roots = _split_paths(values.get("ROUTIVUS_WORKSPACE_ROOTS", ""))
        if not roots:
            roots = (Path.cwd(),)

        hosts = tuple(
            item.strip()
            for item in values.get("ROUTIVUS_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
            if item.strip()
        )
        origins = tuple(
            item.strip()
            for item in values.get("ROUTIVUS_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        ws_auth_token = values.get("ROUTIVUS_SERVER_TOKEN", values.get("ROUTIVUS_WS_AUTH_TOKEN", "")).strip()
        try:
            ws_heartbeat_interval = max(1.0, float(values.get("ROUTIVUS_WS_HEARTBEAT_INTERVAL", "30")))
        except ValueError:
            ws_heartbeat_interval = 30.0
        try:
            ws_max_message_bytes = max(1024, int(values.get("ROUTIVUS_WS_MAX_MESSAGE_BYTES", "1048576")))
        except ValueError:
            ws_max_message_bytes = 1_048_576
        host = values.get("ROUTIVUS_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(values.get("ROUTIVUS_SERVER_PORT", "18765"))
        except ValueError:
            port = 18_765
        # 0 = 由操作系统分配空闲端口（桌面模式用，避免与别的东西撞端口）。
        if not 0 <= port <= 65_535:
            port = 18_765
        static_raw = values.get("ROUTIVUS_STATIC_DIR", "").strip()
        static_dir = Path(static_raw).expanduser() if static_raw else None
        terminal_backend = values.get("ROUTIVUS_TERMINAL_BACKEND", "auto").strip().lower()
        if terminal_backend not in ("auto", "conpty", "oneshot"):
            terminal_backend = "auto"
        return cls(
            projects_file=projects_file,
            database_path=database_path,
            workspace_roots=roots,
            host=host,
            port=port,
            allowed_hosts=hosts,
            allowed_origins=origins,
            ws_auth_token=ws_auth_token,
            ws_heartbeat_interval=ws_heartbeat_interval,
            ws_max_message_bytes=ws_max_message_bytes,
            approval_timeout=_float_env(values, "ROUTIVUS_APPROVAL_TIMEOUT", 300.0, 5.0),
            terminal_enabled=_bool_env(values, "ROUTIVUS_TERMINAL_ENABLED", True),
            terminal_backend=terminal_backend,
            terminal_shell=values.get("ROUTIVUS_TERMINAL_SHELL", "").strip(),
            terminal_max_sessions=_int_env(values, "ROUTIVUS_TERMINAL_MAX_SESSIONS", 4, 1, 64),
            terminal_max_per_project=_int_env(values, "ROUTIVUS_TERMINAL_MAX_PER_PROJECT", 2, 1, 16),
            terminal_idle_timeout=_float_env(values, "ROUTIVUS_TERMINAL_IDLE_TIMEOUT", 900.0, 30.0),
            # 600s 上限对齐 tool/builtin.py 里 execute_command 的超时上限
            terminal_command_timeout=min(600.0, _float_env(values, "ROUTIVUS_TERMINAL_COMMAND_TIMEOUT", 120.0, 1.0)),
            terminal_max_output_bytes=_int_env(values, "ROUTIVUS_TERMINAL_MAX_OUTPUT_BYTES", 262_144, 4_096, 67_108_864),
            terminal_max_input_bytes=_int_env(values, "ROUTIVUS_TERMINAL_MAX_INPUT_BYTES", 65_536, 1_024, ws_max_message_bytes),
            terminal_chunk_bytes=_int_env(values, "ROUTIVUS_TERMINAL_CHUNK_BYTES", 8_192, 512, 65_536),
            terminal_flush_interval=_float_env(values, "ROUTIVUS_TERMINAL_FLUSH_INTERVAL", 0.033, 0.01),
            terminal_queue_max=_int_env(values, "ROUTIVUS_TERMINAL_QUEUE_MAX", 256, 4, 4_096),
            terminal_kill_grace=_float_env(values, "ROUTIVUS_TERMINAL_KILL_GRACE", 3.0, 0.5),
            terminal_cols=_int_env(values, "ROUTIVUS_TERMINAL_COLS", 120, 20, 400),
            terminal_rows=_int_env(values, "ROUTIVUS_TERMINAL_ROWS", 30, 5, 200),
            static_dir=static_dir,
        )
