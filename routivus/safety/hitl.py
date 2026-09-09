"""HITL 审批状态机。

敏感度分级：never（不审）/ confirm（默认审）/ always（必审）。
fail closed：策略启用但无审批回调时，需审批的操作一律拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

DEFAULT_APPROVAL_LEVELS = {
    "read_file": "never",
    "list_dir": "never",
    "glob_files": "never",
    "grep_code": "never",
    "write_file": "confirm",
    "execute_command": "always",
    "web_search": "never",
    "web_fetch": "never",
    # 交互询问无副作用：由 ReAct 在 agent 层拦截等待用户，不在注册表盲执行。
    "ask_user": "never",
}


@dataclass
class ApprovalDecision:
    allow: bool
    args: dict | None = None   # 改参后的参数（allow=True 时生效）
    reason: str = ""           # user_approved / user_rejected / user_skipped / auto_allow / auto_deny_no_requester


ApprovalRequester = Callable[[str, str, dict], Awaitable[ApprovalDecision]]


class HITLPolicy:
    """审批策略：是否启用、敏感度表、会话级全部放行、审批回调。"""

    def __init__(
        self,
        enabled: bool = True,
        levels: dict | None = None,
        requester: ApprovalRequester | None = None,
    ) -> None:
        self.enabled = enabled
        self.levels = dict(DEFAULT_APPROVAL_LEVELS)
        if levels:
            self.levels.update(levels)
        self.requester = requester
        self.session_allow_all = False

    def sensitivity(self, tool_name: str) -> str:
        return self.levels.get(tool_name, "never")

    def requires_approval(self, tool_name: str) -> bool:
        return self.enabled and not self.session_allow_all and self.sensitivity(tool_name) != "never"

    async def decide(self, tool_name: str, args: dict) -> ApprovalDecision:
        """对一次工具调用做审批决策。"""
        if not self.requires_approval(tool_name):
            return ApprovalDecision(allow=True, reason="auto_allow")
        if self.requester is None:
            # fail closed：无交互渠道，需审批的操作一律拒绝
            return ApprovalDecision(allow=False, reason="auto_deny_no_requester")
        return await self.requester(tool_name, self.sensitivity(tool_name), args)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def allow_all(self) -> None:
        """本会话全部放行。"""
        self.session_allow_all = True

    def reset_session(self) -> None:
        self.session_allow_all = False
