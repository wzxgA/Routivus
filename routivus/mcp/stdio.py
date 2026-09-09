"""MCP stdio transport implemented with asyncio subprocesses."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from typing import Any

from routivus.mcp.models import McpServerConfig
from routivus.mcp.protocol import McpProtocolError, McpRequestError, McpUnavailableError, notification_message, request_message
from routivus.mcp.transport import McpTransport


class StdioTransport(McpTransport):
    def __init__(self, config: McpServerConfig) -> None:
        super().__init__(log_lines=config.log_lines)
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._disconnect_notified = False

    async def connect(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if not self.config.command:
            raise McpUnavailableError("stdio Server 缺少 command")
        child_env = os.environ.copy()
        child_env.update(self.config.env)
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                cwd=self.config.cwd,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.config.max_message_bytes + 1,
            )
        except (OSError, ValueError) as exc:
            raise McpUnavailableError(f"无法启动 stdio Server: {exc}") from exc
        self._closing = False
        self._disconnect_notified = False
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"mcp-{self.config.name}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"mcp-{self.config.name}-stderr")
        self._wait_task = asyncio.create_task(self._watch_process(), name=f"mcp-{self.config.name}-wait")

    async def _send(self, message: dict) -> None:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise McpUnavailableError("MCP stdio Server 未连接")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > self.config.max_message_bytes:
            raise McpProtocolError("JSON-RPC 请求超过消息上限")
        async with self._write_lock:
            process.stdin.write(payload)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise McpUnavailableError("MCP stdio 连接已断开") from exc

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(request_message(request_id, method, params))
            message = await asyncio.wait_for(future, timeout=self.config.request_timeout)
        except asyncio.TimeoutError as exc:
            raise McpUnavailableError(f"MCP 请求超时: {method}") from exc
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if isinstance(error, dict):
            raise McpRequestError(error.get("code", -32000), str(error.get("message", "unknown error")), error.get("data"))
        result = message.get("result", {})
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise McpProtocolError("JSON-RPC result 必须是对象")
        return result

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send(notification_message(method, params))

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while not self._closing:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > self.config.max_message_bytes:
                    raise McpProtocolError("MCP stdio 消息超过上限")
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self.log(f"protocol error: invalid JSON ({exc})")
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    self.log("protocol error: invalid JSON-RPC envelope")
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                method = message.get("method")
                if isinstance(method, str) and "id" not in message:
                    await self.dispatch_notification(method, message.get("params"))
                elif isinstance(method, str) and "id" in message:
                    if method == "ping":
                        await self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                    else:
                        await self._send({
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Method not found"},
                        })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"protocol error: {type(exc).__name__}: {exc}")
        finally:
            if not self._closing:
                error = McpUnavailableError("MCP stdio 连接已关闭")
                await self._notify_disconnect(error)

    async def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while not self._closing:
                line = await process.stderr.readline()
                if not line:
                    break
                self.log(line.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise

    async def _watch_process(self) -> None:
        process = self.process
        if process is None:
            return
        code = await process.wait()
        if not self._closing:
            self.log(f"process exited with code {code}")
            error = McpUnavailableError(f"MCP Server 已退出（code={code}）")
            await self._notify_disconnect(error)

    async def _notify_disconnect(self, error: Exception) -> None:
        """Notify the owner once when the child connection is lost.

        Both stdout EOF and process.wait() observe the same child exit. The
        first observer owns failure propagation; the other observer must not
        emit a duplicate lifecycle event.
        """
        if self._closing or self._disconnect_notified:
            return
        self._disconnect_notified = True
        self._fail_pending(error)
        await self.dispatch_disconnect(error)

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._fail_pending(McpUnavailableError("MCP Server 正在关闭"))
        process = self.process
        if process is not None and process.stdin is not None:
            process.stdin.close()
            with suppress(Exception):
                await process.stdin.wait_closed()
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self.config.shutdown_timeout)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.config.shutdown_timeout)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self.process = None
