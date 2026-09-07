"""Ask-User 交互询问单元测试：ReAct 拦截 + fail-closed + 答案回灌。"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from xg.ask.models import AskField, AskOption, AskRequest
from xg.agent.react import AgentEvent, ReActAgent
from xg.llm.client import LlmClient
from xg.llm.types import Message, StreamEvent, ToolCall

ASK_ARGS = {
    "prompt": "我需要先确认一下",
    "fields": [
        {
            "key": "scope",
            "question": "重构范围？",
            "options": [{"label": "仅后端"}, {"label": "全栈", "value": "full"}],
        },
        {"key": "strict", "question": "是否严格模式？", "options": [{"label": "是"}, {"label": "否"}]},
        {"key": "note", "question": "补充说明？", "allow_custom": True},
    ],
}


class ScriptedClient(LlmClient):
    """按脚本依次返回预设响应（content, [(tool_name, args_json), ...]）。"""

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


async def run_events(agent: ReActAgent, user_input: str) -> list[AgentEvent]:
    return [e async for e in agent.run(user_input)]


class TestBuildAskRequest:
    async def test_parses_fields_and_options(self, settings, registry, tmp_project):
        agent = ReActAgent(llm=ScriptedClient([]), tools=registry, settings=settings)
        ask = agent._build_ask_request(ASK_ARGS)

        assert isinstance(ask, AskRequest)
        assert ask.prompt == "我需要先确认一下"
        assert len(ask.fields) == 3

        scope = ask.fields[0]
        assert scope.key == "scope"
        # 未提供 value 时回填 label
        assert scope.options == (AskOption("仅后端", value="仅后端"), AskOption("全栈", value="full"))

        strict = ask.fields[1]
        assert strict.allow_custom is True  # allow_custom 未提供时默认 True

        note = ask.fields[2]
        assert note.allow_custom is True

    async def test_fail_closed_truncation(self, settings, registry, tmp_project):
        agent = ReActAgent(llm=ScriptedClient([]), tools=registry, settings=settings)
        settings.ask_max_fields = 2
        settings.ask_max_options = 1
        ask = agent._build_ask_request(ASK_ARGS)

        assert len(ask.fields) == 2  # 字段截断
        assert len(ask.fields[0].options) == 1  # 选项截断
        # 非法项被跳过（截断发生在过滤前，故合法项须在前 max 条内）
        agent2 = ReActAgent(llm=ScriptedClient([]), tools=registry, settings=settings)
        ask2 = agent2._build_ask_request({"fields": [{"key": "", "question": ""}, {"key": "ok", "question": "q"}]})
        assert len(ask2.fields) == 1
        assert ask2.fields[0].key == "ok"


class TestAskInterception:
    async def test_ask_user_emits_event_and_feeds_answer_back(self, settings, registry):
        asked = {"answers": {}}

        async def requester(ask: AskRequest) -> dict | None:
            asked["request"] = ask
            return {"scope": "full", "strict": "是"}

        script = [
            ("", [("ask_user", '{"prompt": "确认", "fields": [{"key": "scope", "question": "范围？", "options": [{"label": "A"}]}]}')]),
            ("好的，开始重构。", []),
        ]
        agent = ReActAgent(llm=ScriptedClient(script), tools=registry, settings=settings, ask_requester=requester)
        events = await run_events(agent, "重构")

        kinds = [e.kind for e in events]
        assert kinds == ["tool_call", "ask_user", "tool_result", "content", "done"]

        ask_event = next(e for e in events if e.kind == "ask_user")
        assert ask_event.ask is not None
        assert ask_event.ask.fields[0].key == "scope"
        assert asked["request"] is ask_event.ask

        result_event = next(e for e in events if e.kind == "tool_result")
        assert result_event.tool_result.name == "ask_user"
        assert result_event.tool_result.ok is True
        assert '"scope"' in result_event.tool_result.output
        assert '"full"' in result_event.tool_result.output

    async def test_ask_user_skipped_is_fail_closed(self, settings, registry):
        """用户跳过 → 返回循失败（USER_SKIPPED），不代选默认值。"""
        async def requester(ask: AskRequest) -> None:
            return None

        script = [("", [("ask_user", '{"fields": [{"key": "scope", "question": "范围？", "default": "A"}]}')]),
                  ("按默认继续。", [])]
        agent = ReActAgent(llm=ScriptedClient(script), tools=registry, settings=settings, ask_requester=requester)
        events = await run_events(agent, "重构")

        result = next(e for e in events if e.kind == "tool_result")
        assert result.tool_result.ok is False
        assert "USER_SKIPPED" in result.tool_result.error

        # 模型收到的 tool 消息是失败文本，而非默认值
        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        assert tool_msgs[0].content.startswith("ERROR:")

    async def test_ask_user_without_requester_fails_closed(self, settings, registry):
        """非交互会话（无 requester）也 fail-closed，不盲执行。"""
        script = [("", [("ask_user", '{"fields": [{"key": "scope", "question": "范围？"}]}')]),
                  ("继续。", [])]
        agent = ReActAgent(llm=ScriptedClient(script), tools=registry, settings=settings)
        events = await run_events(agent, "重构")

        result = next(e for e in events if e.kind == "tool_result")
        assert result.tool_result.ok is False
        assert "USER_SKIPPED" in result.tool_result.error

    async def test_ask_user_runs_serially_before_parallel_tools(self, settings, registry):
        """ask_user 与普通工具同帧：先串行等答案，普通工具再执行。"""
        async def requester(ask: AskRequest) -> dict | None:
            return {"scope": "后端"}

        script = [
            ("", [("ask_user", '{"fields": [{"key": "scope", "question": "范围？"}]}'), ("list_dir", "{}")]),
            ("完成。", []),
        ]
        agent = ReActAgent(llm=ScriptedClient(script), tools=registry, settings=settings, ask_requester=requester)
        events = await run_events(agent, "重构")

        kinds = [e.kind for e in events]
        # ask_user 先被处理，list_dir 随后照常并行执行
        assert kinds == ["tool_call", "tool_call", "ask_user", "tool_result", "tool_result", "content", "done"]

        ask_result = next(e for e in events if e.kind == "tool_result" and e.tool_result.name == "ask_user")
        assert ask_result.tool_result.ok is True


class TestAskToolRegistered:
    def test_ask_user_tool_in_registry(self, registry):
        tool = registry.get("ask_user")
        assert tool is not None
        assert tool.name == "ask_user"
        assert "fields" in tool.parameters["required"]

    def test_disabled_when_ask_user_enabled_false(self, tmp_project):
        from xg.tool.builtin import build_registry
        reg = build_registry(base_dir=tmp_project, ask_user_enabled=False)
        assert reg.get("ask_user") is None