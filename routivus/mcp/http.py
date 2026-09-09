"""MCP Streamable HTTP transport using httpx."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import AsyncIterator

import httpx

from routivus.mcp.models import McpServerConfig
from routivus.mcp.protocol import McpProtocolError, McpRequestError, McpUnavailableError, notification_message, request_message
from routivus.mcp.transport import McpTransport
from routivus.safety.audit import redact_text


class StreamableHttpTransport(McpTransport):
    def __init__(self, config: McpServerConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(log_lines=config.log_lines)
        self.config = config
        self._external_client = client is not None
        self.client = client
        self.session_id: str | None = None
        self._next_id = 1
        self._notification_task: asyncio.Task | None = None
        self._closed = False

    async def connect(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.request_timeout),
                follow_redirects=True,
                max_redirects=5,
            )
        self._closed = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **dict(self.config.headers),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def request(self, method: str, params: dict | None = None) -> dict:
        if self.client is None or self._closed or not self.config.url:
            raise McpUnavailableError("MCP HTTP Server 未连接")
        request_id = self._next_id
        self._next_id += 1
        response = await self._post(request_message(request_id, method, params))
        messages = await self._messages(response)
        for message in messages:
            if message.get("id") == request_id:
                error = message.get("error")
                if isinstance(error, dict):
                    raise McpRequestError(error.get("code", -32000), str(error.get("message", "unknown error")), error.get("data"))
                result = message.get("result", {})
                if result is None:
                    return {}
                if not isinstance(result, dict):
                    raise McpProtocolError("JSON-RPC result 必须是对象")
                return result
            await self._dispatch_unsolicited(message)
        raise McpProtocolError(f"MCP HTTP 响应缺少 request id {request_id}")

    async def notify(self, method: str, params: dict | None = None) -> None:
        response = await self._post(notification_message(method, params))
        if response.status_code not in (200, 202, 204):
            raise McpUnavailableError(f"MCP HTTP notification 失败: HTTP {response.status_code}")
        if response.content:
            for message in await self._messages(response):
                await self._dispatch_unsolicited(message)

    async def _post(self, payload: dict) -> httpx.Response:
        assert self.client is not None and self.config.url
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.config.max_message_bytes:
            raise McpProtocolError("JSON-RPC 请求超过消息上限")
        try:
            response = await self.client.post(self.config.url, headers=self._headers(), content=encoded)
        except httpx.TimeoutException as exc:
            raise McpUnavailableError("MCP HTTP 请求超时") from exc
        except httpx.HTTPError as exc:
            raise McpUnavailableError(f"MCP HTTP 连接失败: {exc}") from exc
        if response.status_code >= 400:
            preview = redact_text(response.text[:300].replace("\n", " "))
            raise McpUnavailableError(f"MCP HTTP {response.status_code}: {preview}")
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        if len(response.content) > self.config.max_message_bytes:
            raise McpProtocolError("MCP HTTP 响应超过消息上限")
        return response

    async def _messages(self, response: httpx.Response) -> list[dict]:
        if not response.content:
            return []
        content_type = response.headers.get("content-type", "").lower()
        raw_messages: list[object]
        if "text/event-stream" in content_type:
            raw_messages = []
            data_lines: list[str] = []
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    raw_messages.append(json.loads("\n".join(data_lines)))
                    data_lines = []
            if data_lines:
                raw_messages.append(json.loads("\n".join(data_lines)))
        else:
            try:
                parsed = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise McpProtocolError(f"MCP HTTP 返回非法 JSON: {exc}") from exc
            raw_messages = parsed if isinstance(parsed, list) else [parsed]
        messages: list[dict] = []
        for item in raw_messages:
            if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
                raise McpProtocolError("MCP HTTP 返回非法 JSON-RPC envelope")
            messages.append(item)
        return messages

    async def _dispatch_unsolicited(self, message: dict) -> None:
        method = message.get("method")
        if isinstance(method, str) and "id" not in message:
            await self.dispatch_notification(method, message.get("params"))
        elif isinstance(method, str) and "id" in message:
            response = (
                {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                if method == "ping"
                else {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            await self._post(response)

    async def start_notifications(self) -> None:
        if self._notification_task is None and self.client is not None and self.session_id:
            self._notification_task = asyncio.create_task(
                self._listen_notifications(), name=f"mcp-{self.config.name}-http-events"
            )

    async def _listen_notifications(self) -> None:
        assert self.client is not None and self.config.url
        try:
            async with self.client.stream("GET", self.config.url, headers=self._headers()) as response:
                if response.status_code in (405, 404):
                    return
                response.raise_for_status()
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        raw = "\n".join(data_lines)
                        data_lines = []
                        if len(raw.encode("utf-8")) > self.config.max_message_bytes:
                            self.log("notification message exceeds limit")
                            continue
                        message = json.loads(raw)
                        if isinstance(message, dict):
                            await self._dispatch_unsolicited(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self.log(f"notification stream closed: {type(exc).__name__}: {exc}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._notification_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.client is not None and self.config.url and self.session_id:
            with suppress(httpx.HTTPError):
                await self.client.delete(self.config.url, headers=self._headers())
        self.session_id = None
        if self.client is not None and not self._external_client:
            await self.client.aclose()
            self.client = None
