"""LLM 层通用类型：消息、工具调用、流式事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCall:
    """一次工具调用请求（模型侧发起）。"""

    id: str
    name: str
    arguments: str  # JSON 字符串

    def parsed_arguments(self) -> dict[str, Any]:
        import json

        if not self.arguments:
            return {}
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {"_raw": self.arguments}


@dataclass
class ToolResult:
    """一次工具执行结果（工具侧返回，回灌给模型）。"""

    tool_call_id: str
    name: str
    ok: bool
    output: str = ""
    error: str = ""

    def to_message_content(self) -> str:
        if self.ok:
            return self.output or "(no output)"
        return f"ERROR: {self.error or 'unknown error'}"


@dataclass
class Message:
    """对话消息。role ∈ system / user / assistant / tool。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""  # role=tool 时对应的调用 id

    def to_api(self) -> dict[str, Any]:
        """转为 OpenAI Chat Completions API 格式。"""
        msg: dict[str, Any] = {"role": self.role}
        if self.role == "assistant" and self.tool_calls:
            msg["content"] = self.content or None
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        elif self.role == "tool":
            msg["content"] = self.content
            msg["tool_call_id"] = self.tool_call_id
        else:
            msg["content"] = self.content
        return msg


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class StreamEvent:
    """流式事件。

    - kind="content": text 为普通/最终回答增量文本
    - kind="thinking": text 为 Provider 明确返回的思考增量文本
    - kind="tool_call": tool_call 为本轮聚合完成的 ToolCall（一次性发出，不再分片）
    - kind="done": 结束，含 finish_reason 与 usage
    """

    kind: Literal["content", "thinking", "tool_call", "done", "retrying"]
    text: str = ""
    tool_call: ToolCall | None = None
    finish_reason: str = ""
    usage: Usage | None = None
    attempt: int = 0
    max_attempts: int = 0
    retry_after: float | None = None
