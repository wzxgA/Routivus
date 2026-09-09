"""MCP server lifecycle, discovery, dynamic tools and resources."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from routivus.config.mcp import McpConfigManager
from routivus.llm.types import ToolResult
from routivus.mcp.http import StreamableHttpTransport
from routivus.mcp.models import McpEvent, McpResource, McpServerConfig, McpServerSnapshot, McpToolSpec
from routivus.mcp.protocol import DEFAULT_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, McpError, McpProtocolError, McpUnavailableError
from routivus.mcp.resources import decode_resource_contents, find_resource_references, redact_uri
from routivus.mcp.schema import exposed_tool_name, sanitize_description, sanitize_schema
from routivus.mcp.stdio import StdioTransport
from routivus.mcp.transport import McpTransport
from routivus.tool.registry import Tool, ToolRegistry
from routivus.safety.audit import redact_text


EventListener = Callable[[McpEvent], Awaitable[None] | None]
TransportFactory = Callable[[McpServerConfig], McpTransport]


@dataclass
class _ServerRuntime:
    config: McpServerConfig
    status: str = "disabled"
    transport: McpTransport | None = None
    protocol_version: str | None = None
    capabilities: dict = field(default_factory=dict)
    tools: dict[str, McpToolSpec] = field(default_factory=dict)
    resources: dict[str, McpResource] = field(default_factory=dict)
    last_error: str = ""
    started_at: datetime | None = None
    last_refresh_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpManager:
    def __init__(
        self,
        registry: ToolRegistry,
        config_manager: McpConfigManager,
        *,
        approval_policy=None,
        audit=None,
        enabled: bool = True,
        max_servers: int = 32,
        resource_total_chars: int = 64_000,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.registry = registry
        self.config_manager = config_manager
        self.approval_policy = approval_policy
        self.audit = audit
        self.enabled = enabled
        self.max_servers = max(1, max_servers)
        self.resource_total_chars = max(1, resource_total_chars)
        self.transport_factory = transport_factory or self._default_transport
        self._servers: dict[str, _ServerRuntime] = {}
        self._listeners: list[EventListener] = []
        self._started = False
        self._start_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._config_errors: list[str] = []
        self._hitl_names: dict[str, set[str]] = {}
        # A small, intentional adapter hook used by Agent, Plan and commands.
        setattr(self.registry, "mcp_manager", self)

    @staticmethod
    def _default_transport(config: McpServerConfig) -> McpTransport:
        if config.transport == "stdio":
            return StdioTransport(config)
        return StreamableHttpTransport(config)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def config_errors(self) -> tuple[str, ...]:
        return tuple(self._config_errors)

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    async def _emit(self, event: McpEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue

    def _audit(self, action: str, **fields) -> None:
        if self.audit is not None:
            self.audit.record(action, **fields)

    async def ensure_started(self) -> None:
        if not self._started:
            await self.start_all()

    async def start_all(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            if not self.enabled:
                return
            loaded = self.config_manager.load()
            self._config_errors = list(loaded.errors)
            configs = list(loaded.servers.values())
            if len(configs) > self.max_servers:
                self._config_errors.append(
                    f"MCP Server 数量 {len(configs)} 超过上限 {self.max_servers}，仅加载前 {self.max_servers} 个"
                )
                configs = configs[: self.max_servers]
            for config in configs:
                self._servers[config.name] = _ServerRuntime(
                    config=config,
                    status="disabled" if not config.enabled else "starting",
                )
                self._audit(
                    "mcp_server_config_loaded",
                    server=config.name,
                    transport=config.transport,
                    enabled=config.enabled,
                )
            await asyncio.gather(
                *(self.start_server(config.name) for config in configs if config.enabled),
                return_exceptions=True,
            )

    async def start_server(self, name: str) -> bool:
        runtime = self._servers.get(name)
        if runtime is None or not self.enabled:
            return False
        async with runtime.lock:
            if not runtime.config.enabled:
                runtime.status = "disabled"
                return False
            if runtime.status == "ready" and runtime.transport is not None:
                return True
            runtime.status = "starting"
            runtime.last_error = ""
            await self._emit(McpEvent("mcp_server_starting", name))
            self._audit("mcp_server_start", server=name, transport=runtime.config.transport)
            transport = self.transport_factory(runtime.config)
            runtime.transport = transport
            transport.set_notification_handler(
                lambda method, params, server=name: self._on_notification(server, method, params)
            )
            transport.set_disconnect_handler(
                lambda error, server=name: self._on_disconnect(server, error)
            )
            try:
                await asyncio.wait_for(
                    self._initialize(runtime), timeout=runtime.config.startup_timeout
                )
            except asyncio.CancelledError:
                runtime.status = "unavailable"
                runtime.last_error = "MCP Server 启动已取消"
                try:
                    await transport.close()
                finally:
                    runtime.transport = None
                raise
            except Exception as exc:
                # A transport can report the same failure while initialize()
                # is waiting. Route this path through the shared handler so
                # the disconnect callback and startup exception are deduped.
                await self._mark_unavailable(runtime, exc)
                return False
            runtime.status = "ready"
            runtime.started_at = datetime.now()
            runtime.last_refresh_at = runtime.started_at
            self._audit(
                "mcp_server_ready",
                server=name,
                transport=runtime.config.transport,
                protocol_version=runtime.protocol_version,
                tool_count=len(runtime.tools),
                resource_count=len(runtime.resources),
            )
            await self._emit(
                McpEvent(
                    "mcp_server_ready",
                    name,
                    tool_count=len(runtime.tools),
                    resource_count=len(runtime.resources),
                )
            )
            return True

    async def _initialize(self, runtime: _ServerRuntime) -> None:
        assert runtime.transport is not None
        await runtime.transport.connect()
        result = await runtime.transport.request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "routivus", "version": "0.1.0"},
            },
        )
        version = str(result.get("protocolVersion", ""))
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpProtocolError(f"不支持的 MCP protocolVersion: {version or '(empty)'}")
        runtime.protocol_version = version
        runtime.transport.set_protocol_version(version)
        capabilities = result.get("capabilities", {})
        runtime.capabilities = capabilities if isinstance(capabilities, dict) else {}
        await runtime.transport.notify("notifications/initialized")
        if "tools" in runtime.capabilities:
            await self._refresh_tools_locked(runtime)
        else:
            self.registry.replace_source(self._source(runtime.config.name), [])
        if "resources" in runtime.capabilities:
            await self._refresh_resources_locked(runtime, update_registry=False)
            await self._register_tools(runtime)
        await runtime.transport.start_notifications()

    @staticmethod
    def _source(server: str) -> str:
        return f"mcp:{server}"

    async def _paged(self, runtime: _ServerRuntime, method: str, key: str, limit: int) -> list[dict]:
        assert runtime.transport is not None
        values: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        while len(values) < limit:
            params = {"cursor": cursor} if cursor else None
            result = await runtime.transport.request(method, params)
            page = result.get(key, [])
            if not isinstance(page, list):
                raise McpProtocolError(f"{method} 的 {key} 必须是数组")
            values.extend(item for item in page if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in seen:
                raise McpProtocolError(f"{method} 返回重复 cursor")
            seen.add(cursor)
        return values[:limit]

    async def refresh_tools(self, name: str) -> bool:
        runtime = self._servers.get(name)
        if runtime is None or runtime.transport is None:
            return False
        async with runtime.lock:
            if runtime.transport is None or runtime.status in {"disabled", "stopping"}:
                return False
            runtime.status = "refreshing"
            try:
                await self._refresh_tools_locked(runtime)
            except Exception as exc:
                runtime.last_error = self._safe_error(exc)
                runtime.status = "ready"
                await self._emit(McpEvent("mcp_warning", name, runtime.last_error))
                return False
            runtime.status = "ready"
            runtime.last_refresh_at = datetime.now()
            return True

    async def _refresh_tools_locked(self, runtime: _ServerRuntime) -> None:
        raw_tools = await self._paged(
            runtime, "tools/list", "tools", runtime.config.max_tools
        )
        specs: dict[str, McpToolSpec] = {}
        used_exposed: set[str] = set()
        for raw in raw_tools:
            remote_name = str(raw.get("name", "")).strip()
            if not remote_name or len(remote_name) > 128:
                continue
            exposed = exposed_tool_name(runtime.config.name, remote_name)
            if exposed in {
                f"mcp__{runtime.config.name}__resources_list",
                f"mcp__{runtime.config.name}__resources_read",
            }:
                if runtime.transport:
                    runtime.transport.log(f"tool name reserved for resources: {remote_name}")
                continue
            if exposed in used_exposed:
                # Sanitization can collapse distinct remote names.  Keep the
                # first stable mapping and expose the conflict in logs.
                if runtime.transport:
                    runtime.transport.log(f"tool name collision after sanitizing: {remote_name}")
                continue
            schema, warnings = sanitize_schema(raw.get("inputSchema", {}))
            if warnings and runtime.transport:
                runtime.transport.log(f"schema warning {remote_name}: {'; '.join(warnings)}")
            specs[exposed] = McpToolSpec(
                server=runtime.config.name,
                remote_name=remote_name,
                exposed_name=exposed,
                description=sanitize_description(raw.get("description", "")),
                input_schema=schema,
                annotations=raw.get("annotations", {}) if isinstance(raw.get("annotations"), dict) else {},
            )
            used_exposed.add(exposed)
        runtime.tools = specs
        await self._register_tools(runtime)
        self._audit(
            "mcp_tools_refreshed", server=runtime.config.name, tool_count=len(specs)
        )
        await self._emit(
            McpEvent("mcp_tools_refreshed", runtime.config.name, tool_count=len(specs))
        )

    async def _register_tools(self, runtime: _ServerRuntime) -> None:
        tools: list[Tool] = []
        levels: dict[str, str] = {}
        for spec in runtime.tools.values():
            async def handler(args: dict, server=runtime.config.name, remote=spec.remote_name, exposed=spec.exposed_name) -> ToolResult:
                return await self.call_tool(server, remote, exposed, args)

            tools.append(
                Tool(
                    name=spec.exposed_name,
                    description=spec.description or f"MCP tool {spec.remote_name} from {runtime.config.name}",
                    parameters=spec.input_schema,
                    async_handler=handler,
                    source=self._source(runtime.config.name),
                    metadata={"server": runtime.config.name, "remote_name": spec.remote_name},
                )
            )
            levels[spec.exposed_name] = self._approval_level(runtime.config, spec.remote_name)
        if "resources" in runtime.capabilities:
            list_name = f"mcp__{runtime.config.name}__resources_list"
            read_name = f"mcp__{runtime.config.name}__resources_read"

            async def list_handler(args: dict, server=runtime.config.name, name=list_name) -> ToolResult:
                return ToolResult("", name, True, output=self.format_resources(server))

            async def read_handler(args: dict, server=runtime.config.name, name=read_name) -> ToolResult:
                uri = str(args.get("uri", "")).strip()
                if not uri:
                    return ToolResult("", name, False, error="缺少 uri")
                try:
                    text = await self.read_resource(server, uri)
                    return ToolResult("", name, True, output=text)
                except McpError as exc:
                    return ToolResult("", name, False, error=str(exc))

            tools.extend([
                Tool(
                    list_name,
                    f"列出 MCP Server {runtime.config.name} 提供的 resources。",
                    {"type": "object", "properties": {}},
                    async_handler=list_handler,
                    source=self._source(runtime.config.name),
                    metadata={"server": runtime.config.name, "virtual": "resources_list"},
                ),
                Tool(
                    read_name,
                    f"读取 MCP Server {runtime.config.name} 的一个 resource。",
                    {
                        "type": "object",
                        "properties": {"uri": {"type": "string", "description": "资源 URI"}},
                        "required": ["uri"],
                    },
                    async_handler=read_handler,
                    source=self._source(runtime.config.name),
                    metadata={"server": runtime.config.name, "virtual": "resources_read"},
                ),
            ])
            levels[list_name] = "never"
            levels[read_name] = "never"
        self.registry.replace_source(self._source(runtime.config.name), tools)
        self._set_hitl(runtime.config.name, levels)

    def _approval_level(self, config: McpServerConfig, remote_name: str) -> str:
        override = config.tool_overrides.get(remote_name)
        if override:
            return override
        return "confirm" if config.hitl == "default" else config.hitl

    def _set_hitl(self, server: str, levels: dict[str, str]) -> None:
        self._clear_hitl(server)
        if self.approval_policy is None:
            return
        for name, level in levels.items():
            self.approval_policy.levels[name] = level
        self._hitl_names[server] = set(levels)

    def _clear_hitl(self, server: str) -> None:
        names = self._hitl_names.pop(server, set())
        if self.approval_policy is not None:
            for name in names:
                self.approval_policy.levels.pop(name, None)

    async def call_tool(
        self, server: str, remote_name: str, exposed_name: str, args: dict
    ) -> ToolResult:
        runtime = self._servers.get(server)
        if runtime is None or runtime.status not in {"ready", "refreshing"} or runtime.transport is None:
            return ToolResult("", exposed_name, False, error=f"MCP Server {server} 不可用")
        if exposed_name not in runtime.tools:
            return ToolResult("", exposed_name, False, error=f"MCP 工具已不可用: {remote_name}")
        try:
            result = await runtime.transport.request(
                "tools/call", {"name": remote_name, "arguments": args}
            )
        except McpError as exc:
            if isinstance(exc, (McpUnavailableError, McpProtocolError)):
                await self._mark_unavailable(runtime, exc)
            return ToolResult("", exposed_name, False, error=str(exc))
        output_parts: list[str] = []
        content = result.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    output_parts.append(block["text"])
                elif block.get("type") == "resource" and isinstance(block.get("resource"), dict):
                    resource_text, _ = decode_resource_contents(
                        {"contents": [block["resource"]]}, runtime.config.max_output_chars
                    )
                    output_parts.append(resource_text)
                else:
                    output_parts.append(json.dumps(block, ensure_ascii=False))
        structured = result.get("structuredContent")
        if structured is not None:
            output_parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        output = "\n\n".join(part for part in output_parts if part)
        is_error = bool(result.get("isError", False))
        if is_error:
            return ToolResult("", exposed_name, False, error=output or "MCP tool returned an error")
        return ToolResult("", exposed_name, True, output=output or "(no output)")

    async def refresh_resources(self, name: str) -> bool:
        runtime = self._servers.get(name)
        if runtime is None or runtime.transport is None:
            return False
        async with runtime.lock:
            if runtime.transport is None or runtime.status in {"disabled", "stopping"}:
                return False
            previous = dict(runtime.resources)
            runtime.status = "refreshing"
            try:
                await self._refresh_resources_locked(runtime)
            except Exception as exc:
                runtime.resources = previous
                runtime.last_error = self._safe_error(exc)
                runtime.status = "ready"
                await self._emit(McpEvent("mcp_warning", name, runtime.last_error))
                return False
            runtime.status = "ready"
            runtime.last_refresh_at = datetime.now()
            return True

    async def _refresh_resources_locked(
        self, runtime: _ServerRuntime, *, update_registry: bool = True
    ) -> None:
        raw_resources = await self._paged(
            runtime, "resources/list", "resources", runtime.config.max_resources
        )
        resources: dict[str, McpResource] = {}
        for raw in raw_resources:
            uri = str(raw.get("uri", "")).strip()
            if not uri:
                continue
            size = raw.get("size")
            resources[uri] = McpResource(
                server=runtime.config.name,
                uri=uri,
                name=str(raw.get("name") or uri),
                description=sanitize_description(raw.get("description", "")),
                mime_type=str(raw.get("mimeType")) if raw.get("mimeType") else None,
                size=int(size) if isinstance(size, (int, float)) else None,
                metadata={key: value for key, value in raw.items() if key not in {"uri", "name", "description", "mimeType", "size"}},
            )
        runtime.resources = resources
        if update_registry:
            await self._register_tools(runtime)
        self._audit(
            "mcp_resources_refreshed",
            server=runtime.config.name,
            resource_count=len(resources),
        )
        await self._emit(
            McpEvent(
                "mcp_resources_refreshed",
                runtime.config.name,
                resource_count=len(resources),
            )
        )

    async def read_resource(self, server: str, uri: str) -> str:
        runtime = self._servers.get(server)
        if runtime is None or runtime.status not in {"ready", "refreshing"} or runtime.transport is None:
            raise McpUnavailableError(f"MCP Server {server} 不可用")
        if uri not in runtime.resources:
            raise McpUnavailableError(f"MCP Server {server} 未声明 resource: {uri}")
        try:
            result = await runtime.transport.request("resources/read", {"uri": uri})
        except McpError as exc:
            self._audit(
                "mcp_resource_read",
                server=server,
                uri=redact_uri(uri),
                ok=False,
                error=self._safe_error(exc),
            )
            if isinstance(exc, (McpUnavailableError, McpProtocolError)):
                await self._mark_unavailable(runtime, exc)
            raise
        text, _ = decode_resource_contents(result, runtime.config.resource_max_chars)
        if not text:
            text = "(empty resource)"
        self._audit(
            "mcp_resource_read",
            server=server,
            uri=redact_uri(uri),
            chars=len(text),
            ok=True,
        )
        await self._emit(McpEvent("mcp_resource_read", server, redact_uri(uri)))
        return text

    async def expand_references(self, user_input: str) -> str:
        references = find_resource_references(user_input)
        if not references:
            return user_input
        blocks: list[str] = []
        total = 0
        seen: set[tuple[str, str]] = set()
        for reference in references:
            key = (reference.server, reference.uri)
            if key in seen:
                continue
            seen.add(key)
            text = await self.read_resource(reference.server, reference.uri)
            remaining = self.resource_total_chars - total
            if remaining <= 0:
                raise McpProtocolError("当前输入引用的 MCP resources 总量超过上限")
            if len(text) > remaining:
                text = text[:remaining] + "\n... (资源总量达到上限，已截断)"
            total += len(text)
            blocks.append(
                f"[外部 MCP resource: server={reference.server}, uri={redact_uri(reference.uri)}]\n"
                f"--- begin resource content ---\n{text}\n--- end resource content ---"
            )
        return user_input + "\n\n" + "\n\n".join(blocks)

    async def _on_notification(self, server: str, method: str, params: dict) -> None:
        if method in {"notifications/tools/list_changed", "tools/list_changed"}:
            self._schedule(self.refresh_tools(server))
        elif method in {"notifications/resources/list_changed", "resources/list_changed"}:
            self._schedule(self.refresh_resources(server))

    async def _on_disconnect(self, server: str, error: Exception) -> None:
        runtime = self._servers.get(server)
        if runtime is not None:
            await self._mark_unavailable(runtime, error)

    async def _mark_unavailable(self, runtime: _ServerRuntime, error: Exception) -> None:
        if runtime.status in {"disabled", "stopping", "unavailable"}:
            return
        runtime.status = "unavailable"
        runtime.last_error = self._safe_error(error)
        transport = runtime.transport
        runtime.transport = None
        self.registry.unregister_source(self._source(runtime.config.name))
        self._clear_hitl(runtime.config.name)
        self._audit(
            "mcp_server_unavailable",
            server=runtime.config.name,
            transport=runtime.config.transport,
            error=runtime.last_error,
        )
        await self._emit(McpEvent(
            "mcp_server_unavailable", runtime.config.name, runtime.last_error
        ))
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass

    def _schedule(self, awaitable) -> None:
        task = asyncio.create_task(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def snapshots(self) -> list[McpServerSnapshot]:
        return [
            McpServerSnapshot(
                name=name,
                transport=runtime.config.transport,
                status=runtime.status,  # type: ignore[arg-type]
                protocol_version=runtime.protocol_version,
                tool_count=len(runtime.tools),
                resource_count=len(runtime.resources),
                last_error=runtime.last_error,
                started_at=runtime.started_at,
                last_refresh_at=runtime.last_refresh_at,
            )
            for name, runtime in sorted(self._servers.items())
        ]

    def format_status(self) -> str:
        lines: list[str] = []
        if not self.enabled:
            return "MCP 已通过 ROUTIVUS_MCP_ENABLED 关闭。"
        for error in self._config_errors:
            lines.append(f"配置错误: {error}")
        for snapshot in self.snapshots():
            detail = (
                f"{snapshot.name}: {snapshot.status} [{snapshot.transport}] "
                f"tools={snapshot.tool_count} resources={snapshot.resource_count}"
            )
            if snapshot.last_error:
                detail += f" error={snapshot.last_error}"
            lines.append(detail)
        return "\n".join(lines) if lines else "未配置 MCP Server。"

    def format_resources(self, server: str | None = None) -> str:
        lines: list[str] = []
        runtimes = [self._servers[server]] if server in self._servers else self._servers.values() if server is None else []
        for runtime in runtimes:
            for resource in runtime.resources.values():
                size = f" {resource.size}B" if resource.size is not None else ""
                mime = f" {resource.mime_type}" if resource.mime_type else ""
                lines.append(f"{runtime.config.name}: {resource.uri}  {resource.name}{mime}{size}")
        if server is not None and server not in self._servers:
            return f"未知 MCP Server: {server}"
        return "\n".join(lines) if lines else "没有可用的 MCP resources。"

    def logs(self, name: str, limit: int = 50) -> str:
        runtime = self._servers.get(name)
        if runtime is None:
            return f"未知 MCP Server: {name}"
        if runtime.transport is None:
            return runtime.last_error or f"MCP Server {name} 当前没有日志"
        lines = runtime.transport.recent_logs(limit)
        return "\n".join(redact_text(line) for line in lines) if lines else f"MCP Server {name} 当前没有日志"

    async def restart(self, name: str) -> bool:
        if name not in self._servers or not self.enabled:
            return False
        await self.stop_server(name, restarting=True)
        loaded = self.config_manager.load()
        config = loaded.servers.get(name)
        if config is None:
            return False
        runtime = self._servers[name]
        runtime.config = config
        runtime.status = "restarting"
        self._audit("mcp_server_restart", server=name)
        return await self.start_server(name)

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        if enabled and not self.enabled:
            return False
        loaded = self.config_manager.load()
        if name not in loaded.servers and name not in self._servers:
            return False
        self.config_manager.set_enabled(name, enabled)
        refreshed = self.config_manager.load()
        config = refreshed.servers.get(name)
        if config is None:
            return False
        if not enabled:
            if name not in self._servers:
                self._servers[name] = _ServerRuntime(config=config, status="disabled")
            else:
                self._servers[name].config = config
            await self.stop_server(name)
            self._servers[name].status = "disabled"
            return True
        runtime = self._servers.get(name)
        if runtime is None:
            runtime = _ServerRuntime(config=config, status="starting")
            self._servers[name] = runtime
        else:
            runtime.config = config
        return await self.start_server(name)

    async def stop_server(self, name: str, *, restarting: bool = False) -> bool:
        runtime = self._servers.get(name)
        if runtime is None:
            return False
        async with runtime.lock:
            runtime.status = "restarting" if restarting else "stopping"
            transport = runtime.transport
            runtime.transport = None
            self.registry.unregister_source(self._source(name))
            self._clear_hitl(name)
            if transport is not None:
                try:
                    await transport.close()
                except Exception as exc:
                    runtime.last_error = self._safe_error(exc)
            runtime.tools.clear()
            runtime.resources.clear()
            if not restarting:
                runtime.status = "disabled" if not runtime.config.enabled else "unavailable"
            self._audit("mcp_server_stop", server=name)
            await self._emit(McpEvent("mcp_server_stopping", name))
            return True

    async def close(self) -> None:
        for task in tuple(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await asyncio.gather(
            *(self.stop_server(name) for name in list(self._servers)),
            return_exceptions=True,
        )
        self._servers.clear()
        self._started = False

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return redact_text(f"{type(exc).__name__}: {exc}"[:1000])
