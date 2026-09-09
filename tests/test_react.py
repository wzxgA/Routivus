"""ReAct 循环单元测试：mock LlmClient 驱动。"""

from __future__ import annotations

from typing import AsyncIterator

from routivus.agent.react import AgentEvent, ReActAgent
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient, LlmError
from routivus.llm.types import Message, StreamEvent, ToolCall


class ScriptedClient(LlmClient):
    """按脚本依次返回预设响应的 mock 客户端。脚本项为 (content, tool_calls)。"""

    def __init__(self, script: list[tuple[str, list[tuple[str, str]]]]):
        self.script = list(script)
        self.requests: list[list[Message]] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        content, calls = self.script.pop(0)
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call_{name}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


def make_agent(script, settings: Settings, registry) -> ReActAgent:
    return ReActAgent(llm=ScriptedClient(script), tools=registry, settings=settings)


async def run_events(agent: ReActAgent, user_input: str) -> list[AgentEvent]:
    return [e async for e in agent.run(user_input)]


class TestBasicLoop:
    async def test_plain_reply_no_tools(self, settings, registry):
        agent = make_agent([("你好，我是 XG。", [])], settings, registry)
        events = await run_events(agent, "hi")

        kinds = [e.kind for e in events]
        assert kinds == ["content", "done"]
        assert agent.messages[-1].role == "assistant"

    async def test_multi_step_tool_calls(self, settings, registry):
        """三步工具调用链：每步的 tool_result 都正确回灌。"""
        script = [
            ("", [("list_dir", "{}")]),
            ("", [("read_file", '{"path": "README.md"}')]),
            ("任务完成。", []),
        ]
        agent = make_agent(script, settings, registry)
        events = await run_events(agent, "做点什么")

        kinds = [e.kind for e in events]
        assert kinds == [
            "tool_call", "tool_result",
            "tool_call", "tool_result",
            "content", "done",
        ]
        # 消息序列：system + user + (assistant+tool)*2 + assistant
        roles = [m.role for m in agent.messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
        # 第三次请求应包含第二次（read_file）的 tool 结果
        client: ScriptedClient = agent.llm  # type: ignore[assignment]
        assert any("demo" in m.content for m in client.requests[2] if m.role == "tool")


class TestErrorFeedback:
    async def test_tool_error_fed_back(self, settings, registry):
        """工具报错 → 错误文本作为 tool 消息回灌。"""
        script = [
            ("", [("read_file", '{"path": "missing.py"}')]),
            ("文件不存在，我换个方式。", []),
        ]
        agent = make_agent(script, settings, registry)
        events = await run_events(agent, "read missing")

        fail_results = [e for e in events if e.kind == "tool_result" and not e.tool_result.ok]
        assert len(fail_results) == 1
        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        assert tool_msgs[0].content.startswith("ERROR:")

    async def test_unknown_tool_error(self, settings, registry):
        script = [
            ("", [("nonexistent_tool", "{}")]),
            ("好的。", []),
        ]
        agent = make_agent(script, settings, registry)
        events = await run_events(agent, "go")
        assert any(
            e.kind == "tool_result" and "未知工具" in e.tool_result.error for e in events
        )


class TestLimits:
    async def test_step_limit(self, settings, registry):
        """模型持续要求调用工具时，达到步数上限后安全终止。"""
        settings.tool_steps = 3
        endless = [("", [("list_dir", "{}")])] * 10
        agent = make_agent(endless, settings, registry)
        events = await run_events(agent, "无限循环")

        assert events[-1].kind == "step_limit"
        tool_calls = [e for e in events if e.kind == "tool_call"]
        assert len(tool_calls) == 3

    async def test_budget_exceeded(self, settings, registry):
        """上下文 token 估算超预算时终止。"""
        settings.context_window = 100  # budget = 80 token
        script = [("", [("list_dir", "{}")]), ("done", [])]
        agent = make_agent(script, settings, registry)

        # 先塞一段长历史触发预算
        agent.messages.append(Message(role="user", content="x" * 1000))

        events = await run_events(agent, "go")
        assert events[-1].kind == "budget_exceeded"
        assert not any(e.kind == "done" for e in events)


class TestClear:
    async def test_clear_keeps_system_prompt(self, settings, registry):
        agent = make_agent([("ok", [])], settings, registry)
        await run_events(agent, "first")
        agent.clear()
        assert len(agent.messages) == 1
        assert agent.messages[0].role == "system"


async def test_llm_retry_event_and_structured_error_are_forwarded(settings, registry):
    class RetryClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(
                kind="retrying", text="API 临时故障，正在重试",
                attempt=2, max_attempts=3, retry_after=1.0,
            )
            yield StreamEvent(kind="content", text="完成")
            yield StreamEvent(kind="done")

    agent = ReActAgent(llm=RetryClient(), tools=registry, settings=settings)
    events = await run_events(agent, "继续")
    retry = next(event for event in events if event.kind == "retrying")
    assert retry.retry_attempts == 2
    assert retry.retry_max_attempts == 3
    assert [event.kind for event in events] == ["retrying", "content", "done"]

    class ErrorClient(LlmClient):
        async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
            raise LlmError(
                "API 返回 504", category="gateway_timeout", retryable=True,
                attempt=3, max_attempts=3,
            )
            yield StreamEvent(kind="done")

    agent = ReActAgent(llm=ErrorClient(), tools=registry, settings=settings)
    events = await run_events(agent, "继续")
    error = next(event for event in events if event.kind == "error")
    assert error.error_category == "gateway_timeout"
    assert error.retry_attempts == 2
