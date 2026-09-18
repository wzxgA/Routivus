"""任务自然语言续跑的共享骨架（方案 17）。

`routivus/tui/` 与 `routivus/server/` 是并列的上层，两端的"续跑意图识别"与
"可续跑任务登记结构"应该只有一份。共享逻辑下沉到这里，避免两端各写一套慢慢漂移。

设计要点（方案 17 §3.3）：

- **无法判断时选 `new_chat`**（保守）：错判成续跑会"吃掉"用户本想聊的话题，
  比漏判更烦人。
- LLM 调用失败 / 返回非 JSON / 返回非法枚举值 → 全部回退关键词白名单。
- 提示词与关键词与 TUI 保持一致（TUI 目前仍持有一份自己的副本，本期不改 TUI）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from routivus.llm.types import Message

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，避免运行时拖入执行器依赖
    from routivus.agent.plan import PlanExecutor

SYSTEM_RESUME_INTENT_PROMPT = (
    "你负责判断用户输入是针对「未完成任务」的继续指令，还是全新的对话指令。\n"
    "规则：\n"
    "1. 如果用户表达继续/接着/完成刚才那个任务/恢复任务 → 输出 {\"intent\": \"resume_task\"}\n"
    "2. 如果用户提出与待办任务无关的新问题或新指令 → 输出 {\"intent\": \"new_chat\"}\n"
    "无法判断时优先选 new_chat。只输出一行 JSON，不要解释。"
)

ResumeIntent = Literal["resume_task", "new_chat"]

# 关键词白名单：LLM 关闭 / 不可用 / 判不出来时的保守回退（与 TUI 同表）。
_RESUME_KEYWORDS = ("继续", "接着", "继续执行", "continue", "go on", "做完", "把它完成", "恢复任务")


def is_resume_keyword(text: str) -> bool:
    """输入是否命中续跑关键词白名单。"""
    lowered = text.lower()
    return any(word in lowered for word in _RESUME_KEYWORDS)


async def classify_resume_intent(
    llm: Any,
    task_summary: str,
    text: str,
    *,
    enabled: bool = True,
) -> ResumeIntent:
    """LLM 判定输入是否为续跑指令；关闭或失败/输出不合法时回退关键词白名单。

    `task_summary` 是一句话的任务状态概括（"目标：X；3 个子任务，1 失败 2 完成"），
    不要把整份计划塞进来——这里只需要够判断而已。
    """
    if enabled and llm is not None:
        try:
            messages = [
                Message(role="system", content=SYSTEM_RESUME_INTENT_PROMPT),
                Message(
                    role="user",
                    content=f"任务：{task_summary}\n用户输入：{text}\n\n只输出 JSON。",
                ),
            ]
            parts: list[str] = []
            async for event in llm.stream_chat(messages, tools=None):
                if getattr(event, "kind", "") == "content" and getattr(event, "text", ""):
                    parts.append(str(event.text))
            parsed = json.loads("".join(parts))
            if isinstance(parsed, dict):
                kind = str(parsed.get("intent", ""))
                if kind in ("resume_task", "new_chat"):
                    return kind  # type: ignore[return-value]
        except Exception:  # noqa: BLE001 - 识别只是增益，失败必须回退而不是打挂轮次
            pass
    return "resume_task" if is_resume_keyword(text) else "new_chat"


@dataclass
class ResumableTask:
    """最近一次「没跑完、还能接着跑」的任务。一个会话一个槽位，新任务覆盖旧的。

    **槽位在进程内存里，服务重启后清空**。team 还有一条不经过槽位的入口
    （团队卡上的「继续」按钮直接查库），plan 没有——重启后只能重新 `/plan`。

    `plan_executor` 只有 plan 需要：`PlanExecutor.resume()` 依赖内存里的
    `_last_plan`；team 的恢复素材在库里的快照（`team.snapshot`），槽位只存元信息。
    """

    kind: Literal["plan", "team"]
    goal: str
    turn_id: str = ""
    #: 用户主动取消过 → 不再续跑（与 TUI 同语义）。
    user_cancelled: bool = False
    resume_count: int = 0
    plan_executor: "PlanExecutor | None" = None
