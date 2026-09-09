"""集成测试：mock LLM 驱动真实工具，端到端多步任务。"""

from __future__ import annotations

import json
from typing import AsyncIterator

from routivus.agent.react import ReActAgent
from routivus.llm.client import LlmClient
from routivus.llm.types import StreamEvent, ToolCall
from routivus.tool.builtin import build_registry


class TaskScriptClient(LlmClient):
    """按脚本顺序返回响应；记录模型实际收到的完整消息序列。"""

    def __init__(self, script: list[tuple[str, list[tuple[str, str]]]]):
        self.script = list(script)
        self.seen_messages: list[list[dict]] = []

    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        self.seen_messages.append([m.to_api() for m in messages])
        content, calls = self.script.pop(0)
        if content:
            yield StreamEvent(kind="content", text=content)
        for name, args in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(id=f"call_{name}_{len(self.seen_messages)}", name=name, arguments=args),
            )
        yield StreamEvent(kind="done", finish_reason="tool_calls" if calls else "stop")


async def test_end_to_end_multi_step_task(tmp_path, settings):
    """建目录 → 写文件 → 读回验证 → 汇报。"""
    # 第一步：execute_command 建目录
    # 第二步：write_file 写入内容
    # 第三步：read_file 读回
    # 第四步：最终回答
    script = [
        ("", [("execute_command", json.dumps({"command": "mkdir out_dir"}))]),
        ("", [("write_file", json.dumps({"path": "out_dir/result.txt", "content": "hello xg"}))]),
        ("", [("read_file", json.dumps({"path": "out_dir/result.txt"}))]),
        ("任务完成：文件已创建并验证。", []),
    ]
    client = TaskScriptClient(script)
    registry = build_registry(base_dir=tmp_path)
    agent = ReActAgent(llm=client, tools=registry, settings=settings)

    events = [e async for e in agent.run("创建并验证文件")]

    # 文件真实落盘
    result_file = tmp_path / "out_dir" / "result.txt"
    assert result_file.read_text(encoding="utf-8") == "hello xg"

    # 事件流完整：3 组 tool_call/tool_result + 最终 content + done
    kinds = [e.kind for e in events]
    assert kinds == [
        "tool_call", "tool_result",
        "tool_call", "tool_result",
        "tool_call", "tool_result",
        "content", "done",
    ]
    # 所有工具结果回灌正确
    assert events[1].tool_result.ok
    assert events[3].tool_result.ok
    assert events[5].tool_result.ok
    assert "hello xg" in events[5].tool_result.output
    # 每一步请求都携带了前序 tool 消息
    assert len(client.seen_messages) == 4
    tool_msgs_last = [m for m in client.seen_messages[2] if m["role"] == "tool"]
    assert len(tool_msgs_last) == 2


async def test_end_to_end_error_correction(tmp_path, settings):
    """模型读错路径 → 收到错误 → 自行修正路径重读。"""
    script = [
        ("", [("read_file", json.dumps({"path": "src/main.py"}))]),
        ("", [("read_file", json.dumps({"path": "README.md"}))]),
        ("已读取 README。", []),
    ]
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    client = TaskScriptClient(script)
    registry = build_registry(base_dir=tmp_path)
    agent = ReActAgent(llm=client, tools=registry, settings=settings)

    events = [e async for e in agent.run("读 README")]

    first, second = events[0], events[2]
    assert first.tool_call.name == "read_file"
    # 第一次失败（文件不存在），第二次成功
    assert not events[1].tool_result.ok
    assert events[3].tool_result.ok
    assert "# project" in events[3].tool_result.output
    assert events[-1].kind == "done"
