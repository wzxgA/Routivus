"""OpenAI-compatible Chat Completions 实现（httpx SSE 流式 + tool calling 增量聚合）。

所有 provider（GLM / DeepSeek / Kimi / OpenAI 兼容）共享此模板。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

import httpx

from routivus.llm.client import LlmClient, LlmError
from routivus.llm.types import Message, StreamEvent, ToolCall, Usage

DEFAULT_TIMEOUT = 120.0


def _looks_like_html_error(content_type: str = "", body: str = "") -> bool:
    """Identify gateway/proxy error pages, which often use HTTP 400 generically."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    prefix = body.lstrip()[:512].lower()
    return media_type == "text/html" or prefix.startswith(("<!doctype html", "<html", "<head", "<body"))


def _status_retry_policy(
    status_code: int,
    *,
    content_type: str = "",
    body: str = "",
) -> tuple[bool, str]:
    if status_code == 408:
        return True, "transient_api_error"
    if status_code == 429:
        return True, "rate_limited"
    if status_code == 503:
        return True, "service_unavailable"
    if status_code == 504:
        return True, "gateway_timeout"
    if 500 <= status_code < 600:
        return True, "transient_api_error"
    if status_code in {400, 422}:
        if _looks_like_html_error(content_type, body):
            return True, "proxy_bad_request"
        return False, "invalid_request"
    if status_code == 401:
        return False, "authentication_error"
    if status_code == 403:
        return False, "permission_error"
    if status_code == 404:
        return False, "endpoint_not_found"
    if status_code == 413:
        return False, "request_too_large"
    return False, "api_error"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


class OpenAICompatClient(LlmClient):
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT,
        retry_enabled: bool = True,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 8.0,
        retry_jitter: float = 0.25,
        retry_total_timeout: float = 30.0,
        respect_retry_after: bool = True,
    ) -> None:
        # F5：允许以空配置构造（启动不阻断），真正调用时再拦截并给出引导。
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retry_enabled = retry_enabled
        self.max_retries = max(0, max_retries)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self.retry_max_delay = max(0.0, retry_max_delay)
        self.retry_jitter = max(0.0, min(1.0, retry_jitter))
        self.retry_total_timeout = max(0.0, retry_total_timeout)
        self.respect_retry_after = respect_retry_after

    def stream_chat(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        return self._stream_with_retry(messages, tools)

    async def _stream_with_retry(
        self, messages: list[Message], tools: list[dict] | None
    ) -> AsyncIterator[StreamEvent]:
        started_at = time.monotonic()
        max_attempts = 1 + (self.max_retries if self.retry_enabled else 0)
        for attempt in range(1, max_attempts + 1):
            semantic_event_seen = False
            try:
                async for event in self._stream_once(messages, tools):
                    if event.kind in {"content", "thinking", "tool_call"}:
                        semantic_event_seen = True
                    yield event
                return
            except LlmError as error:
                error.response_started = error.response_started or semantic_event_seen
                error.attempt = attempt
                error.max_attempts = max_attempts
                if (
                    not self.retry_enabled
                    or attempt >= max_attempts
                    or not error.retryable
                    or error.response_started
                ):
                    raise
                delay = self._retry_delay(error, attempt)
                elapsed = time.monotonic() - started_at
                if elapsed + delay > self.retry_total_timeout:
                    error.category = f"{error.category}_retry_timeout"
                    raise
                yield StreamEvent(
                    kind="retrying",
                    text="API 临时故障，正在重试",
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    retry_after=delay,
                )
                await asyncio.sleep(delay)

    def _retry_delay(self, error: LlmError, attempt: int) -> float:
        delay = min(self.retry_max_delay, self.retry_base_delay * (2 ** (attempt - 1)))
        if self.respect_retry_after and error.retry_after is not None:
            delay = max(delay, error.retry_after)
        if self.retry_jitter:
            delay *= random.uniform(1.0 - self.retry_jitter, 1.0 + self.retry_jitter)
        return max(0.0, min(delay, self.retry_max_delay))

    async def _stream_once(
        self, messages: list[Message], tools: list[dict] | None
    ) -> AsyncIterator[StreamEvent]:
        if not self.api_base or not self.api_key or not self.model:
            # F5：仅调用 LLM 时拦截，提示在会话内配置即可用，无需重启。
            raise LlmError(
                "缺少可用的 base provider / API Key。请在会话内用 /provider 配置，"
                "例如 /provider add <name> <api_base> --model M --key K --set-base。"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        status = resp.status_code
                        retryable, category = _status_retry_policy(
                            status,
                            content_type=resp.headers.get("Content-Type", ""),
                            body=body,
                        )
                        raise LlmError(
                            f"API 返回 {status}: {body[:500]}",
                            status_code=status,
                            category=category,
                            retryable=retryable,
                            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
                            request_id=resp.headers.get("x-request-id", ""),
                        )
                    async for event in self._parse_sse(resp):
                        yield event
        except httpx.TimeoutException as e:
            raise LlmError(
                f"请求超时（{self.timeout}s）: {e}",
                category="network_timeout", retryable=True,
            ) from e
        except httpx.HTTPError as e:
            raise LlmError(
                f"网络错误: {e}", category="network_error", retryable=True,
            ) from e

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[StreamEvent]:
        """解析 SSE 流，聚合 content 与 tool_call 增量分片。"""
        # 按 index 聚合中的 tool_call 分片
        pending: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage: Usage | None = None

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            if chunk.get("usage"):
                u = chunk["usage"]
                usage = Usage(
                    prompt_tokens=u.get("prompt_tokens", 0),
                    completion_tokens=u.get("completion_tokens", 0),
                    total_tokens=u.get("total_tokens", 0),
                )

            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                # 不同 OpenAI-compatible provider 对显式 reasoning 的字段名
                # 不统一；在 LLM 边界归一化，UI 不读取 provider 私有字段。
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield StreamEvent(kind="thinking", text=str(reasoning))
                if delta.get("content"):
                    yield StreamEvent(kind="content", text=delta["content"])

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    frag = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        frag["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        frag["name"] = frag["name"] + fn["name"]
                    if fn.get("arguments"):
                        frag["arguments"] = frag["arguments"] + fn["arguments"]

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        # 流结束：统一发出聚合后的 tool_call，再发 done
        for idx in sorted(pending):
            frag = pending[idx]
            if not frag["name"] and not frag["arguments"]:
                continue
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(
                    id=frag["id"] or f"call_{idx}",
                    name=frag["name"],
                    arguments=frag["arguments"],
                ),
            )
        yield StreamEvent(kind="done", finish_reason=finish_reason, usage=usage)
