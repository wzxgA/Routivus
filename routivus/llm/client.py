"""LlmClient 抽象基类。所有 provider 实现统一的流式对话接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from routivus.llm.types import Message, StreamEvent


class LlmError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        category: str = "llm_error",
        retryable: bool = False,
        retry_after: float | None = None,
        response_started: bool = False,
        attempt: int = 1,
        max_attempts: int = 1,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.category = category
        self.retryable = retryable
        self.retry_after = retry_after
        self.response_started = response_started
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.request_id = request_id
    """LLM 请求失败（网络 / 鉴权 / 超时 / 协议错误）。"""


class LlmClient(ABC):
    """流式 Chat Completions 客户端抽象。"""

    @abstractmethod
    def stream_chat(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        """流式对话。

        产出事件序列：content* → tool_call* → done。
        失败时抛出 LlmError，消息面向用户可直接展示。
        """
