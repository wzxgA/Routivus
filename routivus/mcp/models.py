"""Data models shared by the MCP configuration and runtime layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping


McpServerStatus = Literal[
    "disabled",
    "starting",
    "ready",
    "refreshing",
    "unavailable",
    "restarting",
    "stopping",
]


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: Literal["stdio", "streamable_http"]
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    startup_timeout: float = 15.0
    request_timeout: float = 120.0
    shutdown_timeout: float = 5.0
    max_output_chars: int = 20_000
    hitl: Literal["default", "never", "confirm", "always"] = "default"
    tool_overrides: Mapping[str, str] = field(default_factory=dict)
    max_tools: int = 256
    max_resources: int = 512
    max_message_bytes: int = 2_097_152
    resource_max_chars: int = 32_000
    log_lines: int = 200


@dataclass(frozen=True)
class McpToolSpec:
    server: str
    remote_name: str
    exposed_name: str
    description: str
    input_schema: dict
    annotations: dict = field(default_factory=dict)


@dataclass(frozen=True)
class McpResource:
    server: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class McpServerSnapshot:
    name: str
    transport: str
    status: McpServerStatus
    protocol_version: str | None = None
    tool_count: int = 0
    resource_count: int = 0
    last_error: str = ""
    started_at: datetime | None = None
    last_refresh_at: datetime | None = None


@dataclass(frozen=True)
class McpEvent:
    kind: str
    server: str = ""
    text: str = ""
    tool_count: int | None = None
    resource_count: int | None = None

