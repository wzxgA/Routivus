"""短期上下文与自动压缩测试。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from routivus.agent.react import ReActAgent
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient
from routivus.llm.types import Message, StreamEvent, ToolCall
from routivus.memory.context import ConversationContext


class SummaryClient(LlmClient):
    def __init__(self, text: str = "已完成：旧任务。") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        yield StreamEvent(kind="content", text=self.text)
        yield StreamEvent(kind="done")


def _context() -> ConversationContext:
    settings = Settings(
        context_window=8_000,
        budget_ratio=0.8,
        context_keep_recent_turns=2,
        context_summary_max_tokens=512,
    )
    return ConversationContext("基础 prompt", settings)


async def test_context_compacts_old_complete_turns_and_preserves_recent():
    context = _context()
    for index in range(6):
        context.append(Message(role="user", content=f"用户任务 {index} " + "旧内容 " * 700))
        context.append(Message(role="assistant", content=f"回复 {index}"))

    client = SummaryClient()
    result = await context.ensure_budget(client)

    assert result.status == "compacted"
    assert result.compressed_turns == 4
    assert context.summary.startswith("已完成")
    users = [m.content for m in context.history if m.role == "user"]
    assert len(users) == 2
    assert "用户任务 4" in users[0]
    assert "用户任务 5" in users[1]


async def test_context_keeps_tool_messages_together_in_summary():
    context = _context()
    context.append(Message(role="user", content="执行检查"))
    context.append(
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path":"a"}')],
        )
    )
    context.append(Message(role="tool", content="file content", tool_call_id="c1"))
    context.append(Message(role="assistant", content="检查完成"))
    for index in range(5):
        context.append(Message(role="user", content=f"新任务 {index} " + "x " * 2500))
        context.append(Message(role="assistant", content="完成"))

    client = SummaryClient()
    result = await context.ensure_budget(client)

    assert result.status == "compacted"
    roles = [message.role for message in context.history]
    assert roles[0] == "system"
    assert "tool" not in roles
    assert '"tool_call_id": "c1"' in client.calls[0][1].content


async def test_context_compression_failure_does_not_mutate_history():
    class FailingClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            raise RuntimeError("summary unavailable")
            yield  # pragma: no cover

    context = _context()
    for index in range(6):
        context.append(Message(role="user", content=f"任务 {index} " + "内容 " * 700))
        context.append(Message(role="assistant", content="完成"))
    before = list(context.history)

    result = await context.ensure_budget(FailingClient())

    assert result.status in ("error", "overflow")
    assert context.history == before
    assert context.summary == ""


# ---------------------------------------------------------------------------
# 中断留下的孤立 tool_calls：不补的话整个会话之后每轮都被 provider 拒
# ---------------------------------------------------------------------------


def _tool_call(call_id: str, name: str = "read_file") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments="{}")


def _paired_in_api(messages: list[Message]) -> bool:
    """复刻 OpenAI 兼容 API 的校验：每个 tool_call_id 都必须有对应的 tool 消息。"""
    api = [message.to_api() for message in messages]
    call_ids = {
        call["id"]
        for message in api
        if message.get("tool_calls")
        for call in message["tool_calls"]
    }
    tool_ids = {message["tool_call_id"] for message in api if message["role"] == "tool"}
    return call_ids <= tool_ids


def test_build_messages_backfills_interrupted_tool_call():
    """工具还没跑完就中断：assistant(tool_calls) 后面没有 tool 消息。

    这是取消落在审批 / 提问 / 工具执行三处 await 上时的真实形态。不补的话请求会被
    provider 拒（insufficient tool messages...），而且因为 agent 跨轮复用，之后每轮都拒。
    """
    context = _context()
    context.append(Message(role="user", content="看看 a.ts"))
    context.append(Message(role="assistant", tool_calls=[_tool_call("c1")]))
    # 模拟中断：没有 tool 消息跟上

    messages = context.build_messages()

    assert _paired_in_api(messages)
    backfilled = [message for message in messages if message.role == "tool"]
    assert [message.tool_call_id for message in backfilled] == ["c1"]
    assert "未执行" in backfilled[0].content


def test_build_messages_only_backfills_the_missing_ones():
    """并行调用只回了一条结果时，已有的结果原样保留，只补缺的那条。"""
    context = _context()
    context.append(Message(role="user", content="并行两个调用"))
    context.append(
        Message(role="assistant", tool_calls=[_tool_call("c1"), _tool_call("c2", "list_dir")])
    )
    context.append(Message(role="tool", content="a.ts 的内容", tool_call_id="c1"))

    messages = context.build_messages()

    by_id = {message.tool_call_id: message.content for message in messages if message.role == "tool"}
    assert by_id["c1"] == "a.ts 的内容"
    assert "未执行" in by_id["c2"]
    assert _paired_in_api(messages)


def test_build_messages_drops_orphan_tool_messages():
    """反向孤立（tool 消息前面没有对应调用）同样会被 API 拒，视图里直接丢掉。"""
    context = _context()
    context.append(Message(role="user", content="普通对话"))
    context.append(Message(role="assistant", content="好的"))
    context.append(Message(role="tool", content="孤儿结果", tool_call_id="ghost"))

    messages = context.build_messages()

    assert [message for message in messages if message.role == "tool"] == []


def test_build_messages_leaves_paired_history_untouched():
    """配对完好的历史一个字都不改（修补必须幂等）。"""
    context = _context()
    context.append(Message(role="user", content="执行检查"))
    context.append(Message(role="assistant", tool_calls=[_tool_call("c1")]))
    context.append(Message(role="tool", content="结果", tool_call_id="c1"))
    context.append(Message(role="assistant", content="完成"))

    messages = context.build_messages()

    assert [message.role for message in messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert messages[-1].content == "完成"
    # history 本身不被改写：补出来的回执只活在"要发出去的那一份"里
    assert len(context.history) == 5


async def test_agent_interrupt_leaves_next_request_valid():
    """端到端复现：工具执行时被取消，随后打包出来的消息仍然合法。

    打的就是 react.py 的中断点——assistant(tool_calls) 已入 history，tool 消息没来得及
    补，这正是用户看到 400 的那一刻。
    """

    class _ToolCallClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(kind="tool_call", tool_call=_tool_call("c1"))
            yield StreamEvent(kind="done")

    class _CancellingTools:
        def schemas(self) -> list[dict]:
            return []

        async def aexecute_calls(self, calls, concurrency=1, timeout=1.0):
            raise asyncio.CancelledError()

    agent = ReActAgent(llm=_ToolCallClient(), tools=_CancellingTools(), settings=Settings())

    with pytest.raises(asyncio.CancelledError):
        async for _event in agent.run("看看 a.ts"):
            pass

    # 中断确实在 history 里留下了孤立的 tool_calls……
    assert any(message.role == "assistant" and message.tool_calls for message in agent.context.history)
    assert [message for message in agent.context.history if message.role == "tool"] == []
    # ……但发给模型的那一份已经补齐，不会再触发 provider 的配对校验
    assert _paired_in_api(agent.context.build_messages())
