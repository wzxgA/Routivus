"""把 HITL 策略桥接到 WebSocket 的审批通道。

ReAct 的审批是**阻塞式**的：`react.py` 里 `await self.approval_policy.decide(...)`
返回之前，生成器不产出任何东西（`AgentEvent(kind="approval")` 是决策之后的通知，
不是暂停点）。所以桥接必须是「回调里 await 一个 Future」，由 WS 收消息循环把
客户端决策 set 进这个 Future —— 与 TUI 的 `_approval_future` / `_ask_future`
（`routivus/tui/controller.py:988-1074`）同构。

fail closed：超时、无待决、并发请求一律拒绝，绝不代用户放行。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from routivus.ask.models import AskRequest
from routivus.safety.hitl import ApprovalDecision

logger = logging.getLogger("routivus.server.approval")

# 事件发出回调：emit(event_type, data)
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
# 进入/退出等待态的钩子：由 app.py 负责更新会话状态并广播 session.status
WaitingFn = Callable[[bool], Awaitable[None]]


def _ask_payload(ask: AskRequest | None) -> dict[str, Any]:
    if ask is None:
        return {}
    try:
        return asdict(ask)
    except TypeError:  # pragma: no cover - 非 dataclass 时的兜底
        return {"id": getattr(ask, "id", ""), "prompt": getattr(ask, "prompt", "")}


@dataclass
class _Pending:
    """当前挂起的一项交互（审批或询问）。同一会话同时只允许一项。"""

    item_id: str
    kind: str  # "approval" | "ask"
    tool_name: str = ""
    # 发给客户端的完整载荷：重连快照用它恢复审批卡，客户端据此继续应答。
    payload: dict = field(default_factory=dict)
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]


class ApprovalBridge:
    """每个会话一个：作为 `HITLPolicy.requester` 与 `agent.ask_requester`。"""

    def __init__(
        self,
        *,
        emit: EmitFn,
        set_waiting: WaitingFn,
        timeout: float = 300.0,
        ask_timeout: float | None = None,
    ) -> None:
        self._emit = emit
        self._set_waiting = set_waiting
        self.timeout = timeout
        self.ask_timeout = timeout if ask_timeout is None else ask_timeout
        self._pending: _Pending | None = None

    # ---------- 只读状态 ----------

    @property
    def pending(self) -> _Pending | None:
        return self._pending

    @property
    def pending_id(self) -> str:
        return self._pending.item_id if self._pending is not None else ""

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    @property
    def pending_payload(self) -> dict[str, Any] | None:
        """待决项的原始事件载荷（重连快照恢复审批卡用）；无待决返回 None。"""
        pending = self._pending
        if pending is None or not pending.payload:
            return None
        return dict(pending.payload)

    # ---------- requester 接口 ----------

    async def request(self, tool_name: str, level: str, args: dict) -> ApprovalDecision:
        """`HITLPolicy.requester`：等待客户端对一个工具调用的批准 / 拒绝。"""
        if self._pending is not None:
            # 同一会话已有待决项：不排队、不代选，直接拒绝这一次调用。
            return ApprovalDecision(allow=False, reason="auto_deny_busy")
        item_id = f"ap-{uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        payload = {
            "kind": "approval",
            "approval_id": item_id,
            "tool_name": tool_name,
            "level": level,
            "arguments": args,
            "timeout": self.timeout,
        }
        self._pending = _Pending(
            item_id=item_id, kind="approval", tool_name=tool_name, payload=payload, future=future
        )
        await self._set_waiting(True)
        await self._emit("approval.requested", payload)
        try:
            # shield：超时时不要让 wait_for 取消掉 future，否则迟到的 resolve
            # 会撞上 InvalidStateError，客户端的决策也就静默丢失了。
            decision = await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout)
        except asyncio.TimeoutError:
            decision = ApprovalDecision(allow=False, reason="approval_timeout")
        finally:
            self._clear(item_id)
            await self._safe_set_waiting(False)
        await self._safe_emit(
            "approval.resolved",
            {
                "kind": "approval",
                "approval_id": item_id,
                "tool_name": tool_name,
                "decision": "approve" if decision.allow else "reject",
                "reason": decision.reason,
                "modified": decision.args is not None,
            },
        )
        return decision

    async def ask(self, ask: AskRequest) -> dict[str, str] | None:
        """`agent.ask_requester`：等待客户端回答 ask_user；跳过则返回 None。"""
        if self._pending is not None:
            return None
        item_id = getattr(ask, "id", "") or f"ask-{uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        payload = {
            "kind": "ask",
            "approval_id": item_id,
            "tool_name": "ask_user",
            "ask": _ask_payload(ask),
            "timeout": self.ask_timeout,
        }
        self._pending = _Pending(
            item_id=item_id, kind="ask", tool_name="ask_user", payload=payload, future=future
        )
        await self._set_waiting(True)
        await self._emit("approval.requested", payload)
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), timeout=self.ask_timeout)
        except asyncio.TimeoutError:
            # fail closed：提问无人应答等同于用户跳过，不代选。
            answer = None
        finally:
            self._clear(item_id)
            await self._safe_set_waiting(False)
        await self._safe_emit(
            "approval.resolved",
            {
                "kind": "ask",
                "approval_id": item_id,
                "tool_name": "ask_user",
                "decision": "answered" if answer is not None else "skipped",
            },
        )
        return answer

    # ---------- 客户端回执 ----------

    def resolve_approval(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """把客户端的审批决策交给挂起中的 `request()`。"""
        pending = self._pending
        if pending is None or pending.kind != "approval":
            return False
        if approval_id and approval_id != pending.item_id:
            return False
        if pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    def resolve_ask(self, ask_id: str, answers: dict[str, str] | None) -> bool:
        """把客户端的问答结果交给挂起中的 `ask()`。"""
        pending = self._pending
        if pending is None or pending.kind != "ask":
            return False
        if ask_id and ask_id != pending.item_id:
            return False
        if pending.future.done():
            return False
        pending.future.set_result(answers)
        return True

    def cancel_pending(self, reason: str = "user_cancelled") -> None:
        """取消会话时解开挂起的交互，避免 Future 永远悬着。

        与 TUI `controller.py:965-986` 的处理一致：审批按拒绝落地，提问按跳过落地。
        """
        pending = self._pending
        if pending is None or pending.future.done():
            return
        if pending.kind == "approval":
            pending.future.set_result(ApprovalDecision(allow=False, reason=reason))
        else:
            pending.future.set_result(None)

    # ---------- 内部 ----------

    def _clear(self, item_id: str) -> None:
        if self._pending is not None and self._pending.item_id == item_id:
            self._pending = None

    async def _safe_emit(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self._emit(event_type, data)
        except Exception:  # pragma: no cover - 连接已断时不该影响决策落地
            logger.debug("emit failed event=%s", event_type, exc_info=True)

    async def _safe_set_waiting(self, waiting: bool) -> None:
        try:
            await self._set_waiting(waiting)
        except Exception:  # pragma: no cover - 同上
            logger.debug("set_waiting failed waiting=%s", waiting, exc_info=True)
