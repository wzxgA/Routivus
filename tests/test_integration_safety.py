"""第 3 期集成测试：并行执行 + HITL 审批 + 策略层端到端。"""

from __future__ import annotations

import json
from typing import AsyncIterator

import pytest

from routivus.agent.react import ReActAgent
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient
from routivus.llm.types import StreamEvent, ToolCall
from routivus.safety.audit import AuditLogger
from routivus.safety.guards import guard_tool_call
from routivus.safety.hitl import ApprovalDecision, HITLPolicy
from routivus.tool.builtin import build_registry


class ScriptedClient(LlmClient):
    def __init__(self, script: list[tuple[str, list[tuple[str, str]]]]):
        self.script = list(script)

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        content, calls = self.script.pop(0)
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call_{name}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


def make_agent(tmp_path, script, requester=None, enabled=True):
    settings = Settings(
        api_base="https://api.test/v1", api_key="k", model="m",
        context_window=128_000, max_parallel=3, tool_timeout=5,
    )
    audit = AuditLogger(tmp_path / ".xg" / "audit.log")
    guard = lambda n, a: guard_tool_call(tmp_path, n, a)  # noqa: E731
    tools = build_registry(base_dir=tmp_path, guard=guard, audit=audit)
    policy = HITLPolicy(enabled=enabled, requester=requester) if requester is not None else None
    return ReActAgent(
        llm=ScriptedClient(script), tools=tools, settings=settings,
        approval_policy=policy, audit=audit,
    )


async def run_events(agent, user_input):
    return [e async for e in agent.run(user_input)]


class TestParallelEndToEnd:
    async def test_three_tool_calls_parallel_ordered(self, tmp_path):
        """一轮 3 个 tool_calls：并行执行且结果按原始顺序回灌。"""
        script = [
            ("", [("write_file", '{"path": "a.txt", "content": "A"}'),
                  ("write_file", '{"path": "b.txt", "content": "B"}'),
                  ("write_file", '{"path": "c.txt", "content": "C"}')]),
            ("完成。", []),
        ]
        agent = make_agent(tmp_path, script)
        events = await run_events(agent, "写三个文件")

        kinds = [e.kind for e in events]
        assert kinds == [
            "tool_call", "tool_call", "tool_call",
            "tool_result", "tool_result", "tool_result",
            "content", "done",
        ]
        results = [e.tool_result for e in events if e.kind == "tool_result"]
        assert all(r.ok for r in results)
        assert [r.tool_call_id for r in results] == ["call_write_file"] * 3
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A"
        assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "C"


class TestHitlEndToEnd:
    async def test_command_approval_denied(self, tmp_path):
        """execute_command 被拒绝 → USER_REJECTED 回灌，命令未执行。"""
        async def deny(tool_name, level, args):
            return ApprovalDecision(allow=False, reason="user_rejected")

        script = [
            ("", [("execute_command", '{"command": "echo should_not_run"}')]),
            ("我换个方式。", []),
        ]
        agent = make_agent(tmp_path, script, requester=deny)
        events = await run_events(agent, "执行命令")

        approvals = [e for e in events if e.kind == "approval"]
        assert len(approvals) == 1
        assert approvals[0].text == "rejected"
        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        assert "USER_REJECTED" in tool_msgs[0].content
        assert events[-1].kind == "done"

    async def test_command_approval_approved_executes(self, tmp_path):
        async def approve(tool_name, level, args):
            return ApprovalDecision(allow=True, reason="user_approved")

        script = [
            ("", [("execute_command", '{"command": "echo hello_xg3"}')]),
            ("执行完成。", []),
        ]
        agent = make_agent(tmp_path, script, requester=approve)
        events = await run_events(agent, "执行命令")

        approvals = [e for e in events if e.kind == "approval"]
        assert approvals[0].text == "approved"
        results = [e.tool_result for e in events if e.kind == "tool_result"]
        assert results[0].ok
        assert "hello_xg3" in results[0].output

    async def test_modified_args_executed(self, tmp_path):
        """改参后执行：用户把 content 改成新值。"""
        async def modify(tool_name, level, args):
            return ApprovalDecision(allow=True, args={"path": "m.txt", "content": "NEW"}, reason="user_modified")

        script = [
            ("", [("write_file", '{"path": "m.txt", "content": "OLD"}')]),
            ("完成。", []),
        ]
        agent = make_agent(tmp_path, script, requester=modify)
        events = await run_events(agent, "写文件")

        approvals = [e for e in events if e.kind == "approval"]
        assert approvals[0].text == "modified"
        assert (tmp_path / "m.txt").read_text(encoding="utf-8") == "NEW"

    async def test_fail_closed_without_requester(self, tmp_path):
        """策略启用但无 requester：需审批的操作被拒绝。"""
        script = [
            ("", [("execute_command", '{"command": "echo x"}')]),
            ("好的。", []),
        ]
        agent = ReActAgent(
            llm=ScriptedClient(script),
            tools=build_registry(base_dir=tmp_path),
            settings=Settings(api_base="https://api.test/v1", api_key="k", model="m", max_parallel=2),
            approval_policy=HITLPolicy(),
        )
        events = await run_events(agent, "执行")
        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        assert "USER_REJECTED" in tool_msgs[0].content


class TestGuardEndToEnd:
    async def test_outside_path_blocked_not_executed(self, tmp_path):
        script = [
            ("", [("read_file", '{"path": "../secret.txt"}')]),
            ("路径不可达，我换个方案。", []),
        ]
        agent = make_agent(tmp_path, script)
        events = await run_events(agent, "读文件")

        results = [e.tool_result for e in events if e.kind == "tool_result"]
        assert not results[0].ok
        assert "策略拒绝" in results[0].error

    async def test_blacklist_command_blocked_even_when_allow_all(self, tmp_path):
        """黑名单命令：即使 HITL 全放行也不执行。"""
        async def approve(tool_name, level, args):
            return ApprovalDecision(allow=True, reason="user_approved")

        script = [
            ("", [("execute_command", '{"command": "rm -rf /"}')]),
            ("失败，换方案。", []),
        ]
        agent = make_agent(tmp_path, script, requester=approve)
        agent.approval_policy.allow_all()  # 全放行也不能绕过策略层
        events = await run_events(agent, "执行")

        results = [e.tool_result for e in events if e.kind == "tool_result"]
        assert not results[0].ok
        assert "策略拒绝" in results[0].error
        assert "command_blacklist" in results[0].error

    async def test_audit_contains_blocked_and_approval(self, tmp_path):
        import json as j

        async def deny(tool_name, level, args):
            return ApprovalDecision(allow=False, reason="user_rejected")

        script = [
            ("", [("execute_command", '{"command": "rm -rf /"}'),
                  ("read_file", '{"path": "../x.txt"}'),
                  ("read_file", '{"path": "a.py"}')]),
            ("完成。", []),
        ]
        agent = make_agent(tmp_path, script, requester=deny)
        await run_events(agent, "任务")

        lines = (tmp_path / ".xg" / "audit.log").read_text(encoding="utf-8").strip().splitlines()
        actions = [j.loads(line)["action"] for line in lines]
        assert "blocked" in actions          # 黑名单 + 越界路径
        assert "approval" in actions         # HITL 决策
        assert "tool_call" in actions        # 实际执行
