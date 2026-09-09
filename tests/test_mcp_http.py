"""Streamable HTTP JSON/SSE/session behavior."""

from __future__ import annotations

import json

import httpx

from routivus.mcp.http import StreamableHttpTransport
from routivus.mcp.models import McpServerConfig


async def test_http_json_response_and_session_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        headers = {"content-type": "application/json"}
        if len(seen) == 1:
            headers["Mcp-Session-Id"] = "session-1"
        return httpx.Response(
            200,
            headers=headers,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = McpServerConfig("remote", "streamable_http", url="https://example.test/mcp")
    transport = StreamableHttpTransport(config, client=client)
    await transport.connect()
    assert await transport.request("ping") == {"ok": True}
    transport.set_protocol_version("2025-11-25")
    assert await transport.request("ping") == {"ok": True}
    assert seen[1].headers["mcp-session-id"] == "session-1"
    assert seen[1].headers["mcp-protocol-version"] == "2025-11-25"
    await transport.close()
    await client.aclose()


async def test_http_sse_response_and_notification_dispatch():
    notifications = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        body = (
            'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}\n\n'
            f'data: {{"jsonrpc":"2.0","id":{payload["id"]},"result":{{"pong":true}}}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = McpServerConfig("remote", "streamable_http", url="https://example.test/mcp")
    transport = StreamableHttpTransport(config, client=client)
    transport.set_notification_handler(lambda method, params: notifications.append(method))
    await transport.connect()
    assert await transport.request("ping") == {"pong": True}
    assert notifications == ["notifications/tools/list_changed"]
    await transport.close()
    await client.aclose()
