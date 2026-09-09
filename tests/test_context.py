"""短期上下文与自动压缩测试。"""

from __future__ import annotations

from typing import AsyncIterator

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
