"""把计划 / 团队审阅桥接到 WebSocket。

`PlanExecutor` / `TeamExecutor` 的 `reviewer` 是**阻塞式**回调：返回
`ReviewDecision` 之前事件流不产出任何东西（`PlanEvent(kind="review")` 是即将
等待的通知，不是暂停点）。所以桥接必须是「回调里 await 一个 Future」，由 WS
收消息循环把客户端决策 set 进这个 Future —— 与 HITL 审批（`approval.py`）和
TUI 的 `_review_future`（`routivus/tui/controller.py:1076-1088`）同构。

fail closed：超时、无待决、并发请求一律按「取消」落地，绝不代用户批准执行。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from routivus.agent.plan import ReviewDecision

logger = logging.getLogger("routivus.server.plan_review")

# 事件发出回调：emit(event_type, data)
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
# 计划视图序列化：view(plan, mode) -> JSON 可编码的字典
ViewFn = Callable[[Any, str], dict[str, Any]]

ACTIONS = ("execute", "cancel", "replan")


@dataclass
class _PendingReview:
    review_id: str
    # 发给客户端的完整载荷（含计划视图）：重连快照用它恢复审阅卡，客户端据此继续应答。
    payload: dict = field(default_factory=dict)
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]


class PlanReviewBridge:
    """每个会话一个：作为 `PlanExecutor` / `TeamExecutor` 的 `reviewer`。

    `mode` 由开轮方在调用前设置（"plan" / "team"），用于把同一份计划快照
    标成对的卡片类型，前端才能渲染成计划卡或团队卡。
    """

    def __init__(
        self,
        *,
        emit: EmitFn,
        view: ViewFn,
        timeout: float = 300.0,
        mode: str = "plan",
    ) -> None:
        self._emit = emit
        self._view = view
        self.timeout = timeout
        self.mode = mode
        self._pending: _PendingReview | None = None

    # ---------- 只读状态 ----------

    @property
    def pending(self) -> _PendingReview | None:
        return self._pending

    @property
    def pending_id(self) -> str:
        return self._pending.review_id if self._pending is not None else ""

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    @property
    def pending_payload(self) -> dict[str, Any] | None:
        """待决审阅的原始事件载荷（重连快照恢复审阅卡用）；无待决返回 None。"""
        pending = self._pending
        if pending is None or not pending.payload:
            return None
        return dict(pending.payload)

    # ---------- reviewer 接口 ----------

    async def review(self, plan: Any) -> ReviewDecision:
        """`PlanReviewer`：等待客户端对计划的执行 / 取消 / 重新规划决策。"""
        if self._pending is not None:
            # 同一会话已有待决项：不排队、不代选，直接取消这一次审阅。
            return ReviewDecision(action="cancel", feedback="review_busy")
        review_id = f"rv-{uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        payload = {
            "kind": "review",
            "review_id": review_id,
            "mode": self.mode,
            "plan": self._safe_view(plan, self.mode),
            "timeout": self.timeout,
        }
        self._pending = _PendingReview(review_id=review_id, payload=payload, future=future)
        await self._safe_emit("plan.review", payload)
        try:
            # shield：超时时不要让 wait_for 取消掉 future，否则迟到的 resolve
            # 会撞上 InvalidStateError，客户端的决策也就静默丢失了。
            decision = await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout)
        except asyncio.TimeoutError:
            decision = ReviewDecision(action="cancel", feedback="review_timeout")
            logger.info("plan review timed out review_id=%s mode=%s", review_id, self.mode)
        finally:
            self._clear(review_id)
        await self._safe_emit(
            "plan.review_resolved",
            {
                "kind": "review_resolved",
                "review_id": review_id,
                "mode": self.mode,
                "action": decision.action,
                "feedback": decision.feedback,
            },
        )
        return decision

    # ---------- 客户端回执 ----------

    def resolve(self, review_id: str, action: str, feedback: str = "") -> bool:
        """把客户端的审阅决策交给挂起中的 `review()`。"""
        pending = self._pending
        if pending is None:
            return False
        if review_id and review_id != pending.review_id:
            return False
        if pending.future.done():
            return False
        if action not in ACTIONS:
            return False
        pending.future.set_result(ReviewDecision(action=action, feedback=feedback))  # type: ignore[arg-type]
        return True

    def cancel_pending(self, reason: str = "user_cancelled") -> None:
        """取消会话时解开挂起的审阅，避免 Future 永远悬着。

        与 TUI 取消路径一致：按「取消计划」落地，不执行任何工具。
        """
        pending = self._pending
        if pending is None or pending.future.done():
            return
        pending.future.set_result(ReviewDecision(action="cancel", feedback=reason))

    # ---------- 内部 ----------

    def _clear(self, review_id: str) -> None:
        if self._pending is not None and self._pending.review_id == review_id:
            self._pending = None

    def _safe_view(self, plan: Any, mode: str) -> dict[str, Any]:
        try:
            return self._view(plan, mode)
        except Exception:  # pragma: no cover - 序列化失败不该挡住审阅往返
            logger.debug("plan view failed mode=%s", mode, exc_info=True)
            return {}

    async def _safe_emit(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self._emit(event_type, data)
        except Exception:  # pragma: no cover - 连接已断时不该影响决策落地
            logger.debug("emit failed event=%s", event_type, exc_info=True)
