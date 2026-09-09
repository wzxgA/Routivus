"""ToolRegistry：工具注册、schema 导出、按原始 tool_call 顺序执行。

- execute_calls：同步顺序执行（兼容旧调用）
- aexecute_calls：asyncio 并行执行，默认 4 并发，统一超时/取消兜底，结果保序
- 可选接入策略层（guard）与审计（audit），策略层拒绝为终审
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from routivus.llm.types import ToolCall, ToolResult

GuardFn = Callable[[str, dict], Any]  # (tool_name, args) -> GuardResult


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema（properties / required）
    handler: Callable[[dict], ToolResult] | None = None
    async_handler: Callable[[dict], Awaitable[ToolResult]] | None = None
    source: str = "builtin"
    metadata: dict[str, Any] | None = None


class ToolRegistry:
    def __init__(
        self,
        max_output_chars: int = 20_000,
        guard: GuardFn | None = None,
        audit=None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self.max_output_chars = max_output_chars
        self.guard = guard
        self.audit = audit

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_source(self, source: str) -> list[str]:
        """Remove all tools supplied by one dynamic source."""
        removed = [name for name, tool in self._tools.items() if tool.source == source]
        for name in removed:
            self._tools.pop(name, None)
        return removed

    def replace_source(self, source: str, tools: list[Tool]) -> None:
        """Atomically replace tools from ``source`` after collision validation."""
        incoming: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in incoming:
                raise ValueError(f"重复工具名: {tool.name}")
            existing = self._tools.get(tool.name)
            if existing is not None and existing.source != source:
                raise ValueError(f"工具名冲突: {tool.name}（来源 {existing.source}）")
            incoming[tool.name] = tool
        retained = {
            name: tool for name, tool in self._tools.items() if tool.source != source
        }
        retained.update(incoming)
        self._tools = retained

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """导出为 OpenAI tools 参数所需的 function 定义列表。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict) -> ToolResult:
        """策略校验 → 工具查找 → 执行 → 审计。"""
        if self.guard is not None:
            verdict = self.guard(name, args)
            if not getattr(verdict, "ok", True):
                if self.audit is not None:
                    self.audit.blocked(reason=verdict.reason, tool=name, args=args)
                return ToolResult(
                    tool_call_id="", name=name, ok=False,
                    error=f"策略拒绝（{verdict.reason}）: {verdict.detail}",
                )

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool_call_id="", name=name, ok=False,
                error=f"未知工具: {name}，可用工具: {', '.join(self._tools)}",
            )
        if tool.async_handler is not None and tool.handler is None:
            return ToolResult(
                tool_call_id="", name=name, ok=False,
                error="异步工具不能通过同步执行入口调用",
            )
        started = time.monotonic()
        try:
            assert tool.handler is not None
            result = tool.handler(args)
        except Exception as e:  # 工具内部异常统一转为 error 回灌
            result = ToolResult(tool_call_id="", name=name, ok=False, error=f"{type(e).__name__}: {e}")
        if len(result.output) > self.max_output_chars:
            result.output = (
                result.output[: self.max_output_chars]
                + f"\n... (输出已截断，原始长度 {len(result.output)} 字符)"
            )
        if len(result.error) > self.max_output_chars:
            result.error = (
                result.error[: self.max_output_chars]
                + f"\n... (错误已截断，原始长度 {len(result.error)} 字符)"
            )
        if self.audit is not None:
            self.audit.tool_call(
                tool=name,
                args=args,
                ok=result.ok,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return result

    async def aexecute(self, name: str, args: dict) -> ToolResult:
        """Execute either an async-native tool or a synchronous tool safely."""
        if self.guard is not None:
            verdict = self.guard(name, args)
            if not getattr(verdict, "ok", True):
                if self.audit is not None:
                    self.audit.blocked(reason=verdict.reason, tool=name, args=args)
                return ToolResult(
                    tool_call_id="", name=name, ok=False,
                    error=f"策略拒绝（{verdict.reason}）: {verdict.detail}",
                )

        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool_call_id="", name=name, ok=False,
                error=f"未知工具: {name}，可用工具: {', '.join(self._tools)}",
            )
        started = time.monotonic()
        try:
            if tool.async_handler is not None:
                result = await tool.async_handler(args)
            elif tool.handler is not None:
                result = await asyncio.to_thread(tool.handler, args)
            else:
                result = ToolResult(
                    tool_call_id="", name=name, ok=False, error="工具没有可执行 handler"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = ToolResult(
                tool_call_id="", name=name, ok=False, error=f"{type(e).__name__}: {e}"
            )
        if len(result.output) > self.max_output_chars:
            result.output = (
                result.output[: self.max_output_chars]
                + f"\n... (输出已截断，原始长度 {len(result.output)} 字符)"
            )
        if len(result.error) > self.max_output_chars:
            result.error = (
                result.error[: self.max_output_chars]
                + f"\n... (错误已截断，原始长度 {len(result.error)} 字符)"
            )
        if self.audit is not None:
            self.audit.tool_call(
                tool=name,
                args=args,
                ok=result.ok,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return result

    def execute_calls(self, calls: list[ToolCall]) -> list[ToolResult]:
        """按原始 tool_call 顺序执行（结果保序）。"""
        results: list[ToolResult] = []
        for call in calls:
            result = self.execute(call.name, call.parsed_arguments())
            result.tool_call_id = call.id
            results.append(result)
        return results

    async def aexecute_calls(
        self,
        calls: list[ToolCall],
        concurrency: int = 4,
        timeout: float = 120.0,
    ) -> list[ToolResult]:
        """asyncio 并行执行，结果按原始顺序返回（gather 天然保序）。"""
        sem = asyncio.Semaphore(concurrency)

        async def run(call: ToolCall) -> ToolResult:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self.aexecute(call.name, call.parsed_arguments()), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, ok=False,
                        error=f"工具超时（{timeout}s）",
                    )
                except asyncio.CancelledError:
                    return ToolResult(
                        tool_call_id=call.id, name=call.name, ok=False, error="已取消"
                    )

        results = await asyncio.gather(*(run(c) for c in calls))
        for call, result in zip(calls, results):
            result.tool_call_id = call.id
        return results
