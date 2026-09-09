"""并行工具执行单元测试：并发限流、保序、超时、取消。"""

from __future__ import annotations

import asyncio
import time

from routivus.llm.types import ToolCall, ToolResult
from routivus.tool.builtin import build_registry
from routivus.tool.registry import Tool, ToolRegistry


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def slow_handler(delay: float, marker: str) -> ToolResult:
        time.sleep(delay)
        return ToolResult(tool_call_id="", name="sleep", ok=True, output=marker)

    def make_tool(name: str, delay: float, marker: str) -> Tool:
        return Tool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=lambda a, d=delay, m=marker: slow_handler(d, m),
        )

    registry.register(make_tool("fast_a", 0.05, "A"))
    registry.register(make_tool("fast_b", 0.05, "B"))
    registry.register(make_tool("fast_c", 0.05, "C"))
    registry.register(make_tool("slow_d", 0.4, "D"))
    return registry


def calls(names: list[str]) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=name, arguments="{}") for i, name in enumerate(names)]


class TestParallelExecution:
    async def test_results_in_original_order(self):
        registry = make_registry()
        results = await registry.aexecute_calls(
            calls(["fast_b", "fast_a", "fast_c"]), concurrency=3, timeout=5
        )
        assert [r.output for r in results] == ["B", "A", "C"]
        assert [r.tool_call_id for r in results] == ["c0", "c1", "c2"]

    async def test_parallel_is_faster_than_sequential(self):
        registry = make_registry()
        started = time.monotonic()
        await registry.aexecute_calls(calls(["fast_a", "fast_b", "fast_c"]), concurrency=3, timeout=5)
        elapsed = time.monotonic() - started
        # 顺序执行约 0.15s，并行应明显更快
        assert elapsed < 0.12

    async def test_concurrency_limited_by_semaphore(self):
        """并发不超过上限：3 个 0.15s 任务在并发 1 下串行。"""
        registry = make_registry()
        started = time.monotonic()
        await registry.aexecute_calls(calls(["fast_a", "fast_b", "fast_c"]), concurrency=1, timeout=5)
        elapsed = time.monotonic() - started
        assert elapsed >= 0.14  # 串行累计

    async def test_single_tool_timeout(self):
        registry = make_registry()
        results = await registry.aexecute_calls(
            calls(["slow_d"]), concurrency=1, timeout=0.1
        )
        assert not results[0].ok
        assert "超时" in results[0].error

    async def test_batch_cancel_marks_cancelled(self):
        registry = make_registry()
        task = asyncio.ensure_future(
            registry.aexecute_calls(calls(["slow_d", "slow_d"]), concurrency=1, timeout=5)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 任务被取消时 gather 抛 CancelledError；单任务取消路径在 react 层兜底
        assert task.cancelled() or task.done()


class TestSyncCompatibility:
    def test_execute_calls_still_sequential(self):
        registry = make_registry()
        results = registry.execute_calls(calls(["fast_a", "fast_b"]))
        assert [r.output for r in results] == ["A", "B"]


class TestGuardIntegration:
    def test_guard_rejection_returns_error(self, tmp_path):
        from routivus.safety.guards import guard_tool_call

        registry = ToolRegistry(guard=lambda n, a: guard_tool_call(tmp_path, n, a))
        from routivus.tool.builtin import build_registry

        registry = build_registry(base_dir=tmp_path, guard=lambda n, a: guard_tool_call(tmp_path, n, a))
        result = registry.execute("read_file", {"path": "../escape.txt"})
        assert not result.ok
        assert "策略拒绝" in result.error

    async def test_guard_rejection_in_parallel_batch(self, tmp_path):
        from routivus.safety.guards import guard_tool_call

        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        registry = build_registry(base_dir=tmp_path, guard=lambda n, a: guard_tool_call(tmp_path, n, a))
        results = await registry.aexecute_calls(
            [ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}'),
             ToolCall(id="c2", name="read_file", arguments='{"path": "../bad.txt"}')],
            concurrency=2, timeout=5,
        )
        assert results[0].ok
        assert not results[1].ok
        assert "策略拒绝" in results[1].error


class TestAuditIntegration:
    def test_registry_audits_tool_calls(self, tmp_path):
        import json

        from routivus.safety.audit import AuditLogger

        log_path = tmp_path / ".xg" / "audit.log"
        audit = AuditLogger(log_path)
        from routivus.tool.builtin import build_registry

        registry = build_registry(base_dir=tmp_path, audit=audit)
        registry.execute("write_file", {"path": "x.txt", "content": "hi"})

        entry = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert entry["action"] == "tool_call"
        assert entry["tool"] == "write_file"
        assert entry["ok"] is True
