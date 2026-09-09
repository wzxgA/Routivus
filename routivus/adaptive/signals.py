"""隐式信号的运行时判定与采集。

判定用短关键词列表（不走 LLM），与 rule_router 特征共用同一条"中文子串匹配"
思路。这里只负责"识别 + 交给 FeedbackRecorder"，不触碰路由/UI。

四类可采集信号：
- interrupt        回答中途 Ctrl+C（inline 主循环 KeyboardInterrupt 处记录）
- clarify          上轮后紧接追问/否定，且上轮档 ≤Superior
- cmd_retry        紧接重试类输入
- short_high_tier  简短闲聊却落到中高档（本轮档 ≥Enhanced）

红线：采集只发生在调用方显式触发时；开关关闭时调用方根本不会调用本模块，
因此不产生任何磁盘写。
"""

from __future__ import annotations

from .feedback import FeedbackRecorder, SignalType

# 简短文本判定阈值（字符数）
SHORT_TEXT_CHARS = 60

# 追问/否定词（clarify 触发：上轮后紧接这类输入）
_FOLLOWUP_WORDS = (
    "不对", "不行", "不是这个", "再改", "再调", "重新来", "换一种", "这样呢",
    "继续", "然后呢", "那接下来",
    "no", "not this", "again", "instead", "rewrite", "redesign", "try again",
)

# 重试类词（cmd_retry 触发；与 clarify 部分重叠，但语义聚焦"重新执行"）
_RETRY_WORDS = (
    "重来", "再来", "重新试", "再试一次", "重做",
    "retry", "redo", "again", "try once more",
)


def _short_and_simple(features: dict) -> bool:
    """是否"简短闲聊/简单问答"（short_high_tier 的文本侧条件）。"""
    return bool(features.get("is_chatty") or features.get("question_mark"))


def detect_clarify(text: str, prev_tier: str | None, TIER_NAMES: list[str]) -> tuple[bool, int]:
    """上轮后紧接追问/否定，且上轮档 ≤Superior。返回 (是否触发, 上轮档索引)。"""
    if prev_tier is None:
        return False, -1
    try:
        prev_idx = TIER_NAMES.index(prev_tier)
    except ValueError:
        return False, -1
    low = text.lower()
    hit = any(w in low for w in _FOLLOWUP_WORDS)
    # ≤Superior => 索引 <= 2
    return (hit and prev_idx <= 2), prev_idx


def detect_cmd_retry(text: str, prev_tier: str | None) -> bool:
    """紧接重试类输入（需有上一轮）。"""
    if prev_tier is None:
        return False
    low = text.lower()
    return any(w in low for w in _RETRY_WORDS)


def detect_short_high_tier(text: str, features: dict, cur_tier: str | None,
                           TIER_NAMES: list[str]) -> bool:
    """简短闲聊却落到中高档（本轮档 ≥Enhanced，即索引 >= 1）。"""
    if cur_tier is None:
        return False
    try:
        cur_idx = TIER_NAMES.index(cur_tier)
    except ValueError:
        return False
    if cur_idx < 1:
        return False
    if len(text) > SHORT_TEXT_CHARS:
        return False
    return _short_and_simple(features)


def capture_turn_signals(
    recorder: FeedbackRecorder,
    text: str,
    features: dict,
    prev_tier: str | None,
    cur_tier: str,
    TIER_NAMES: list[str],
) -> list[SignalType]:
    """对一轮普通输入判定四类信号（不含 interrupt）并采集到 recorder。

    返回实际采集的信号列表，便于测试断言。clarify / cmd_retry 用上轮档，
    short_high_tier 用本轮档（cur_tier）。
    """
    emitted: list[SignalType] = []

    clarify, _ = detect_clarify(text, prev_tier, TIER_NAMES)
    if clarify:
        recorder.capture(SignalType.CLARIFY, model_tier=prev_tier or cur_tier,
                         text=text, features=features)
        emitted.append(SignalType.CLARIFY)

    if detect_cmd_retry(text, prev_tier):
        recorder.capture(SignalType.CMD_RETRY, model_tier=prev_tier or cur_tier,
                         text=text, features=features)
        emitted.append(SignalType.CMD_RETRY)

    if detect_short_high_tier(text, features, cur_tier, TIER_NAMES):
        recorder.capture(SignalType.SHORT_HIGH_TIER, model_tier=cur_tier,
                         text=text, features=features)
        emitted.append(SignalType.SHORT_HIGH_TIER)

    return emitted


def capture_interrupt(
    recorder: FeedbackRecorder,
    cur_tier: str | None,
) -> bool:
    """inline 主循环在 KeyboardInterrupt 处调用，记录 interrupt 信号。

    需上一个已路由档位 cur_tier 作为 model_tier；无档位则跳过。
    """
    if cur_tier is None:
        return False
    recorder.capture(SignalType.INTERRUPT, model_tier=cur_tier)
    return True