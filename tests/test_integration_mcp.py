"""MCP end-to-end integration through ReAct and shared commands."""

from __future__ import annotations

import json
import sys
from typing import AsyncIterator

from routivus.agent.react import ReActAgent
from routivus.cli.commands import CommandContext, CommandService
from routivus.config.mcp import McpConfigManager
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient
from routivus.llm.types import StreamEvent, ToolCall
from routivus.mcp.manager import McpManager
from routivus.mcp.http import StreamableHttpTransport
from routivus.mcp.transport import McpTransport
from routivus.safety.hitl import HITLPolicy
from routivus.tool.registry import ToolRegistry
import httpx


class IntegrationTransport(McpTransport):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.tool_calls = []

    async def connect(self):
        pass

    async def request(self, method, params=None):
        if method == "initialize":
            return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        if method == "tools/list":
            return {"tools": [{
                "name": "lookup",
                "description": "look up a value",
                "inputSchema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }]}
        if method == "tools/call":
            self.tool_calls.append(params)
            return {"content": [{"type": "text", "text": "found:42"}]}
        raise AssertionError(method)

    async def notify(self, method, params=None):
        pass

    async def close(self):
        pass


class McpCallingLlm(LlmClient):
    def __init__(self):
        self.round = 0

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.round += 1
        if self.round == 1:
            assert any(tool["name"] == "mcp__demo__lookup" for tool in tools)
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall("mcp-call-1", "mcp__demo__lookup", '{"key":"answer"}'),
            )
            yield StreamEvent(kind="done")
            return
        assert any(message.role == "tool" and "found:42" in message.content for message in messages)
        yield StreamEvent(kind="content", text="结果是 42")
        yield StreamEvent(kind="done")


async def test_react_discovers_and_executes_mcp_tool(tmp_path):
    config_path = tmp_path / ".routivus" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "servers": {"demo": {"transport": "stdio", "command": "fake"}}
    }), encoding="utf-8")
    registry = ToolRegistry()
    hitl = HITLPolicy(enabled=False)
    transports = []

    def factory(config):
        transport = IntegrationTransport(config)
        transports.append(transport)
        return transport

    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
        approval_policy=hitl,
        transport_factory=factory,
    )
    agent = ReActAgent(
        McpCallingLlm(), registry, Settings(), approval_policy=hitl, mcp_manager=manager
    )
    events = [event async for event in agent.run("lookup the answer")]
    assert any(event.kind == "tool_result" and event.tool_result and event.tool_result.ok for event in events)
    assert any(event.kind == "content" and "42" in event.text for event in events)
    assert transports[0].tool_calls == [{"name": "lookup", "arguments": {"key": "answer"}}]

    service = CommandService(CommandContext(agent, Settings(), object()))
    result = await service.execute("/mcp status")
    assert result.ok
    assert "demo: ready" in result.message
    await manager.close()


async def test_real_stdio_server_tools_and_resources_end_to_end(tmp_path):
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{}, "resources":{}}}
    elif method == "tools/list":
        result = {"tools":[{"name":"echo", "description":"echo", "inputSchema":{"type":"object", "properties":{"value":{"type":"string"}}, "required":["value"]}}]}
    elif method == "resources/list":
        result = {"resources":[{"uri":"test://guide", "name":"guide", "mimeType":"text/plain"}]}
    elif method == "tools/call":
        result = {"content":[{"type":"text", "text":"stdio:" + msg["params"]["arguments"]["value"]}]}
    elif method == "resources/read":
        result = {"contents":[{"uri":"test://guide", "text":"stdio resource"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0", "id":msg["id"], "result":result}), flush=True)
'''
    config_path = tmp_path / ".routivus" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"servers": {"stdio": {
        "transport": "stdio", "command": sys.executable, "args": ["-u", "-c", script]
    }}}), encoding="utf-8")
    registry = ToolRegistry()
    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
    )
    await manager.start_all()
    result = await registry.aexecute("mcp__stdio__echo", {"value": "ok"})
    assert result.ok and result.output == "stdio:ok"
    assert await manager.read_resource("stdio", "test://guide") == "stdio resource"
    await manager.close()


async def test_real_streamable_http_tools_and_resources_end_to_end(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{}, "resources":{}}}
        elif method == "tools/list":
            result = {"tools":[{"name":"echo", "description":"echo", "inputSchema":{"type":"object", "properties":{"value":{"type":"string"}}}}]}
        elif method == "resources/list":
            result = {"resources":[{"uri":"test://http-guide", "name":"guide"}]}
        elif method == "tools/call":
            result = {"content":[{"type":"text", "text":"http:" + msg["params"]["arguments"]["value"]}]}
        elif method == "resources/read":
            result = {"contents":[{"uri":"test://http-guide", "text":"http resource"}]}
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc":"2.0", "id":msg["id"], "result":result})

    config_path = tmp_path / ".routivus" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"servers": {"remote": {
        "transport": "streamable_http", "url": "https://example.test/mcp"
    }}}), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ToolRegistry()
    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
        transport_factory=lambda config: StreamableHttpTransport(config, client=client),
    )
    await manager.start_all()
    result = await registry.aexecute("mcp__remote__echo", {"value": "ok"})
    assert result.ok and result.output == "http:ok"
    assert await manager.read_resource("remote", "test://http-guide") == "http resource"
    await manager.close()
    await client.aclose()
