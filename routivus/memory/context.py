"""短期对话上下文、动态预算和滚动摘要。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Awaitable, Callable

from routivus.config.settings import Settings
from routivus.llm.client import LlmClient
from routivus.llm.types import Message
from routivus.memory.models import CompressionResult, SharedSection


SharedProvider = Callable[[], list[SharedSection]]


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n... [context truncated] ...\n"
    if limit <= len(marker) + 2:
        return text[:limit]
    left = (limit - len(marker)) // 2
    right = limit - len(marker) - left
    return text[:left] + marker + text[-right:]


class ConversationContext:
    """保留兼容历史列表的上下文管理器。

    ``history`` 的第一个元素始终是基础 system message；项目/长期记忆和摘要
    只在 ``build_messages`` 时装配，因此现有调用方直接追加 ``agent.messages``
    仍然有效。
    """

    def __init__(
        self,
        system_prompt: str,
        settings: Settings,
        shared_provider: SharedProvider | None = None,
    ) -> None:
        self.settings = settings
        self.base_system_prompt = Message(role="system", content=system_prompt)
        self.history: list[Message] = [self.base_system_prompt]
        self.summary = ""
        self.shared_sections: list[SharedSection] = []
        self.shared_provider = shared_provider
        self._warned_sources: set[str] = set()

    def append(self, message: Message) -> None:
        self.history.append(message)

    def clear(self) -> None:
        self.history[:] = [self.base_system_prompt]
        self.summary = ""

    def _refresh_shared(self) -> list[str]:
        warnings: list[str] = []
        if self.shared_provider is None:
            self.shared_sections = []
            return warnings
        try:
            self.shared_sections = list(self.shared_provider())
        except Exception as exc:  # 记忆不可用不能阻断普通对话
            self.shared_sections = []
            warnings.append(f"读取共享记忆失败：{exc}")
        return warnings

    def build_messages(self) -> list[Message]:
        messages = [self.base_system_prompt]
        for section in self.shared_sections:
            messages.append(
                Message(
                    role="system",
                    content=(
                        f"[共享记忆来源：{section.source}]\n"
                        f"<project_memory source=\"{section.source}\">\n"
                        f"{section.text}\n</project_memory>"
                    ),
                )
            )
        if self.summary:
            messages.append(
                Message(
                    role="system",
                    content="[历史对话摘要；摘要可能有损，请以最近原文为准]\n" + self.summary,
                )
            )
        messages.extend(self.history[1:])
        return messages

    def _message_tokens(self, message: Message) -> int:
        total = 4 + self.settings.estimate_tokens(message.content)
        total += self.settings.estimate_tokens(message.tool_call_id)
        for call in message.tool_calls:
            total += 4 + self.settings.estimate_tokens(
                call.id + call.name + call.arguments
            )
        return total

    def estimate_request_tokens(self, tool_schemas: list[dict] | None = None) -> int:
        total = sum(self._message_tokens(message) for message in self.build_messages())
        if tool_schemas:
            total += 8 + self.settings.estimate_tokens(
                json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True)
            )
        return total

    def _budget_values(self) -> tuple[int, int, int]:
        window = max(1, int(self.settings.context_window))
        response_reserve = min(max(int(window * 0.15), 2_048), 16_384)
        safety_buffer = min(max(int(window * 0.05), 1_024), 8_192)
        summary_reserve = min(
            max(int(window * 0.05), 512),
            max(512, int(self.settings.context_summary_max_tokens)),
        )
        request_limit = min(
            int(window * self.settings.budget_ratio),
            max(1, window - response_reserve - safety_buffer),
        )
        return request_limit, max(1, request_limit - summary_reserve), summary_reserve

    def _truncate_to_tokens(self, text: str, target: int) -> str:
        if target <= 0:
            return ""
        if self.settings.estimate_tokens(text) <= target:
            return text
        low, high = 1, len(text)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = _head_tail(text, mid)
            if self.settings.estimate_tokens(candidate) <= target:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _fit_shared_sections(self, tool_schemas: list[dict], request_limit: int) -> list[str]:
        """按请求预算限制静态项目/长期记忆，避免小窗口被其占满。"""
        fixed = self._message_tokens(self.base_system_prompt)
        if tool_schemas:
            fixed += 8 + self.settings.estimate_tokens(
                json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True)
            )
        persistent_limit = min(
            int(request_limit * 0.30), max(0, request_limit - fixed)
        )
        if not self.shared_sections or persistent_limit <= 0:
            self.shared_sections = [] if persistent_limit <= 0 else self.shared_sections
            return [] if persistent_limit <= 0 else []

        weights = []
        for section in self.shared_sections:
            if section.source == "XG.md":
                weights.append(0.50)
            elif section.source == "XG.local.md":
                weights.append(0.25)
            else:
                weights.append(0.25)
        weight_total = sum(weights) or 1.0
        sections: list[SharedSection] = []
        warnings: list[str] = []
        for section, weight in zip(self.shared_sections, weights):
            target = max(1, int(persistent_limit * weight / weight_total))
            clipped = self._truncate_to_tokens(section.text, target)
            if clipped != section.text and section.source not in self._warned_sources:
                warnings.append(
                    f"{section.source} 已按当前上下文预算截断"
                )
                self._warned_sources.add(section.source)
            if clipped:
                sections.append(replace(section, text=clipped))
        self.shared_sections = sections
        return warnings

    def _blocks(self) -> list[list[Message]]:
        blocks: list[list[Message]] = []
        current: list[Message] = []
        for message in self.history[1:]:
            if message.role == "user" and current:
                blocks.append(current)
                current = []
            current.append(message)
        if current:
            blocks.append(current)
        return blocks

    @staticmethod
    def _serialize_blocks(blocks: list[list[Message]]) -> str:
        lines: list[str] = []
        for index, block in enumerate(blocks, 1):
            lines.append(f"\n--- 对话轮次 {index} ---")
            for message in block:
                payload = message.to_api()
                lines.append(json.dumps(payload, ensure_ascii=False))
        return "\n".join(lines)

    async def _compress(
        self,
        llm: LlmClient,
        blocks: list[list[Message]],
        keep_recent: int,
        summary_reserve: int,
        request_limit: int,
    ) -> tuple[bool, int, str]:
        if len(blocks) <= keep_recent:
            return False, 0, "没有可压缩的已完成对话轮次"
        old_blocks = blocks if keep_recent == 0 else blocks[:-keep_recent]
        if not old_blocks:
            return False, 0, "没有可压缩的已完成对话轮次"
        # 分块摘要，避免极长历史本身超过摘要请求的窗口。分块只影响摘要输入，
        # 原始消息仍按完整轮次原子保留/删除。
        chunk_limit = max(1_024, request_limit - summary_reserve)
        chunks: list[list[list[Message]]] = []
        current: list[list[Message]] = []
        for block in old_blocks:
            candidate = self._serialize_blocks(current + [block])
            if current and self.settings.estimate_tokens(candidate) > chunk_limit:
                chunks.append(current)
                current = []
            current.append(block)
        if current:
            chunks.append(current)

        rolling_summary = self.summary
        for chunk in chunks:
            old_text = self._serialize_blocks(chunk)
            if self.settings.estimate_tokens(old_text) > chunk_limit:
                old_text = self._truncate_to_tokens(old_text, chunk_limit)
            prompt = (
                "请把下面的旧对话合并为一份滚动摘要。只根据原文，不要编造。\n"
                "必须保留用户约束、已完成/失败状态、精确路径、符号名、命令、错误和下一步。\n"
                "不要输出密钥、token 或 Authorization 值。严格使用以下标题：\n"
                "目标与用户约束：\n关键决定：\n已完成工作：\n"
                "文件/接口/命令等精确信息：\n未完成事项与下一步：\n重要错误与工具结果：\n\n"
                f"旧滚动摘要：\n{rolling_summary or '（无）'}\n\n旧对话：\n{old_text}"
            )
            messages = [
                Message(
                    role="system",
                    content="你是 XG-CLI 的上下文压缩器，只输出摘要正文，不调用工具。",
                ),
                Message(role="user", content=prompt),
            ]
            summary = ""
            last_error = ""
            for _attempt in range(2):
                try:
                    parts: list[str] = []
                    async for event in llm.stream_chat(messages, tools=None):
                        if event.kind == "content" and event.text:
                            parts.append(event.text)
                    summary = "".join(parts).strip()
                    if summary:
                        break
                    last_error = "返回为空"
                except Exception as exc:
                    last_error = str(exc)
            if not summary:
                return False, 0, f"摘要生成失败：{last_error}"
            rolling_summary = self._truncate_to_tokens(summary, summary_reserve)

        new_history = [self.base_system_prompt]
        recent_blocks = [] if keep_recent == 0 else blocks[-keep_recent:]
        for block in recent_blocks:
            new_history.extend(block)
        self.history[:] = new_history
        self.summary = rolling_summary
        return True, len(old_blocks), rolling_summary

    async def ensure_budget(
        self, llm: LlmClient, tool_schemas: list[dict] | None = None
    ) -> CompressionResult:
        tool_schemas = tool_schemas or []
        warnings = self._refresh_shared()
        request_limit, trigger, summary_reserve = self._budget_values()
        warnings.extend(self._fit_shared_sections(tool_schemas, request_limit))
        before = self.estimate_request_tokens(tool_schemas)
        if before <= trigger:
            status = "warning" if warnings else "ready"
            return CompressionResult(
                status=status,
                before_tokens=before,
                after_tokens=before,
                request_token_limit=request_limit,
                message="；".join(warnings),
                warnings=tuple(warnings),
            )

        blocks = self._blocks()
        keep_recent = max(0, int(self.settings.context_keep_recent_turns))
        compressed, turns, detail = await self._compress(
            llm, blocks, keep_recent, summary_reserve, request_limit
        )
        after = self.estimate_request_tokens(tool_schemas)
        if compressed and after <= request_limit:
            return CompressionResult(
                status="compacted",
                before_tokens=before,
                after_tokens=after,
                request_token_limit=request_limit,
                compressed_turns=turns,
                message=f"上下文已压缩：{before} → {after} token，合并 {turns} 轮",
                warnings=tuple(warnings),
            )
        if compressed and after > request_limit:
            detail = f"压缩后仍超出输入预算（{after}/{request_limit} token）"
        if not compressed and before <= request_limit:
            return CompressionResult(
                status="error",
                before_tokens=before,
                after_tokens=before,
                request_token_limit=request_limit,
                message=detail,
                warnings=tuple(warnings),
            )
        return CompressionResult(
            status="overflow",
            before_tokens=before,
            after_tokens=after,
            request_token_limit=request_limit,
            message=detail or f"上下文超出输入预算（{after}/{request_limit} token）",
            warnings=tuple(warnings),
        )
