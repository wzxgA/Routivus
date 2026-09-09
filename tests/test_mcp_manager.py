"""MCP lifecycle, dynamic tools, resources, notifications and isolation."""

from __future__ import annotations

import asyncio
import json

from routivus.config.mcp import McpConfigManager
from routivus.llm.types import ToolCall
from routivus.mcp.manager import McpManager
from routivus.mcp.protocol import McpUnavailableError
from routivus.mcp.transport import McpTransport
from routivus.safety.hitl import HITLPolicy
from routivus.tool.registry import ToolRegistry


class FakeTransport(McpTransport):
    def __init__(self, config, *, fail=False):
        super().__init__()
        self.config = config
        self.fail = fail
        self.connected = False
        self.closed = False
        self.calls = []
        self.tools = [{
            "name": "echo.value",
            "description": "Echo a value",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }]
        self.resources = [{
            "uri": "file:///guide.md",
            "name": "guide",
            "mimeType": "text/markdown",
        }]

    async def connect(self):
        if self.fail:
            raise McpUnavailableError("fixture unavailable")
        self.connected = True

    async def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True}},
                "serverInfo": {"name": self.config.name, "version": "1"},
            }
        if method == "tools/list":
            return {"tools": list(self.tools)}
        if method == "resources/list":
            return {"resources": list(self.resources)}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": f"echo:{params['arguments']['value']}"}]}
        if method == "resources/read":
            return {"contents": [{"uri": params["uri"], "mimeType": "text/markdown", "text": "# MCP Guide"}]}
        raise AssertionError(method)

    async def notify(self, method, params=None):
        self.calls.append((method, params))

    async def close(self):
        self.closed = True


class DisconnectingTransport(FakeTransport):
    async def request(self, method, params=None):
        if method == "initialize":
            error = McpUnavailableError("fixture connection closed")
            await self.dispatch_disconnect(error)
            raise error
        return await super().request(method, params)


def _write_config(tmp_path, servers):
    path = tmp_path / ".xg" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")


def _manager(tmp_path, servers, *, failing=()):
    _write_config(tmp_path, servers)
    registry = ToolRegistry()
    hitl = HITLPolicy()
    transports = {}

    def factory(config):
        transport = FakeTransport(config, fail=config.name in failing)
        transports[config.name] = transport
        return transport

    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
        approval_policy=hitl,
        transport_factory=factory,
    )
    return manager, registry, hitl, transports


async def test_discovers_tools_calls_them_and_registers_hitl(tmp_path):
    manager, registry, hitl, transports = _manager(
        tmp_path, {"demo": {"transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    assert manager.snapshots()[0].status == "ready"
    assert "mcp__demo__echo_value" in registry.names()
    assert "mcp__demo__resources_read" in registry.names()
    assert hitl.sensitivity("mcp__demo__echo_value") == "confirm"
    assert hitl.sensitivity("mcp__demo__resources_read") == "never"

    results = await registry.aexecute_calls([
        ToolCall("c1", "mcp__demo__echo_value", '{"value":"hello"}')
    ])
    assert results[0].ok
    assert results[0].output == "echo:hello"
    assert ("tools/call", {"name": "echo.value", "arguments": {"value": "hello"}}) in transports["demo"].calls
    await manager.close()
    assert "mcp__demo__echo_value" not in registry.names()


async def test_resources_and_explicit_reference_expansion(tmp_path):
    manager, _, _, _ = _manager(
        tmp_path, {"docs": {"transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    assert "file:///guide.md" in manager.format_resources("docs")
    expanded = await manager.expand_references("Review @docs:file:///guide.md")
    assert "Review @docs:file:///guide.md" in expanded
    assert "# MCP Guide" in expanded
    assert "外部 MCP resource" in expanded
    await manager.close()


async def test_list_changed_atomically_replaces_server_tools(tmp_path):
    manager, registry, _, transports = _manager(
        tmp_path, {"demo": {"transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    transport = transports["demo"]
    transport.tools = [{
        "name": "new-tool",
        "description": "new",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    await transport.dispatch_notification("notifications/tools/list_changed", {})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "mcp__demo__new-tool" in registry.names()
    assert "mcp__demo__echo_value" not in registry.names()
    await manager.close()


async def test_one_server_failure_does_not_break_healthy_server(tmp_path):
    manager, registry, _, _ = _manager(
        tmp_path,
        {
            "bad": {"transport": "stdio", "command": "fake"},
            "good": {"transport": "stdio", "command": "fake"},
        },
        failing={"bad"},
    )
    await manager.start_all()
    states = {item.name: item.status for item in manager.snapshots()}
    assert states == {"bad": "unavailable", "good": "ready"}
    assert "mcp__good__echo_value" in registry.names()
    assert "mcp__bad__echo_value" not in registry.names()
    await manager.close()


async def test_startup_disconnect_is_reported_once(tmp_path):
    _write_config(tmp_path, {"demo": {"transport": "stdio", "command": "fake"}})
    registry = ToolRegistry()
    events = []

    def factory(config):
        return DisconnectingTransport(config)

    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
        transport_factory=factory,
    )
    manager.add_listener(lambda event: events.append(event))

    await manager.start_all()

    unavailable = [event for event in events if event.kind == "mcp_server_unavailable"]
    assert len(unavailable) == 1
    assert manager.snapshots()[0].status == "unavailable"
    await manager.close()


async def test_disable_persists_and_unregisters_tools(tmp_path):
    manager, registry, _, _ = _manager(
        tmp_path, {"demo": {"transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    assert await manager.set_enabled("demo", False)
    assert manager.snapshots()[0].status == "disabled"
    assert not any(name.startswith("mcp__demo__") for name in registry.names())
    assert McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}).load().servers["demo"].enabled is False
    await manager.close()


async def test_restart_replaces_transport_and_rediscovers_tools(tmp_path):
    manager, registry, _, transports = _manager(
        tmp_path, {"demo": {"transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    first = transports["demo"]
    assert await manager.restart("demo")
    assert first.closed
    assert transports["demo"] is not first
    assert "mcp__demo__echo_value" in registry.names()
    await manager.close()


async def test_disabled_server_can_be_enabled_at_runtime(tmp_path):
    manager, registry, _, _ = _manager(
        tmp_path, {"demo": {"enabled": False, "transport": "stdio", "command": "fake"}}
    )
    await manager.start_all()
    assert manager.snapshots()[0].status == "disabled"
    assert await manager.set_enabled("demo", True)
    assert manager.snapshots()[0].status == "ready"
    assert "mcp__demo__echo_value" in registry.names()
    await manager.close()
