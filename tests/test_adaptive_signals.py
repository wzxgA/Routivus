"""自适应信号判定与采集挂点单测。

重点：
- 四类信号的触发/不触发边界（含权重 panic：权重应由 SIGNAL_META 决定，不在此处写死）；
- off 时零采集零写盘（由 controller/app 挂点保证，这里测 signals 纯函数本身）；
- capture_turn_signals 返回实际采集列表，便于断言。
"""

from __future__ import annotations

import pytest

from routivus.adaptive.feedback import FeedbackRecorder, SignalType
from routivus.adaptive.signals import (
    capture_interrupt,
    capture_turn_signals,
    detect_clarify,
    detect_cmd_retry,
    detect_short_high_tier,
)

TIER_NAMES = ["Basic", "Enhanced", "Superior", "Ultimate"]


@pytest.fixture()
def rec(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))
    return FeedbackRecorder(session="proj-x")


# ---------- clarify ----------

def test_clarify_triggers_on_followup_below_superior(rec):
    emitted = capture_turn_signals(rec, "不对，再改一下", {}, prev_tier="Enhanced", cur_tier="Enhanced", TIER_NAMES=TIER_NAMES)
    assert SignalType.CLARIFY in emitted


def test_clarify_no_prev_tier_no_signal(rec):
    emitted = capture_turn_signals(rec, "不对，再改一下", {}, prev_tier=None, cur_tier="Basic", TIER_NAMES=TIER_NAMES)
    assert SignalType.CLARIFY not in emitted


def test_clarify_suppressed_at_ultimate_prev(rec):
    # 上轮已是 Ultimate（>Superior 索引 2）
    emitted = capture_turn_signals(rec, "不对", {}, prev_tier="Ultimate", cur_tier="Ultimate", TIER_NAMES=TIER_NAMES)
    assert SignalType.CLARIFY not in emitted


def test_clarify_no_followup_word_no_signal(rec):
    emitted = capture_turn_signals(rec, "写个二分查找", {}, prev_tier="Enhanced", cur_tier="Enhanced", TIER_NAMES=TIER_NAMES)
    assert SignalType.CLARIFY not in emitted


# ---------- cmd_retry ----------

def test_cmd_retry_triggers(rec):
    emitted = capture_turn_signals(rec, "重来，再做一次", {}, prev_tier="Superior", cur_tier="Superior", TIER_NAMES=TIER_NAMES)
    assert SignalType.CMD_RETRY in emitted


def test_cmd_retry_requires_prev_tier(rec):
    emitted = capture_turn_signals(rec, "重来", {}, prev_tier=None, cur_tier="Basic", TIER_NAMES=TIER_NAMES)
    assert SignalType.CMD_RETRY not in emitted


# ---------- short_high_tier ----------

def test_short_chatty_on_high_tier_triggers(rec):
    emitted = capture_turn_signals(rec, "你好", {"is_chatty": 1}, prev_tier=None, cur_tier="Superior", TIER_NAMES=TIER_NAMES)
    assert SignalType.SHORT_HIGH_TIER in emitted


def test_short_chatty_on_basic_no_signal(rec):
    emitted = capture_turn_signals(rec, "你好", {"is_chatty": 1}, prev_tier=None, cur_tier="Basic", TIER_NAMES=TIER_NAMES)
    assert SignalType.SHORT_HIGH_TIER not in emitted


def test_long_text_no_signal(rec):
    emitted = capture_turn_signals(rec, "你好" * 60, {"is_chatty": 1}, prev_tier=None, cur_tier="Superior", TIER_NAMES=TIER_NAMES)
    assert SignalType.SHORT_HIGH_TIER not in emitted


def test_non_chatty_no_signal(rec):
    emitted = capture_turn_signals(rec, "你好", {"is_chatty": 0, "question_mark": 0}, prev_tier=None, cur_tier="Superior", TIER_NAMES=TIER_NAMES)
    assert SignalType.SHORT_HIGH_TIER not in emitted


# ---------- 权重 encode 由 SIGNAL_META 决定（验证写盘值） ----------

def test_clarify_weight_persisted(rec):
    capture_turn_signals(rec, "不对", {}, prev_tier="Enhanced", cur_tier="Enhanced", TIER_NAMES=TIER_NAMES)
    rec.flush()
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.store import feedback_log_path
    rows = read_feedback(feedback_log_path())
    assert rows and rows[0]["source"] == "clarify"
    assert rows[0]["weight"] == 1.0      # SIGNAL_META 定义
    assert rows[0]["signal"] == "upgrade"


def test_short_high_tier_is_downgrade(rec):
    capture_turn_signals(rec, "你好", {"is_chatty": 1}, prev_tier=None, cur_tier="Superior", TIER_NAMES=TIER_NAMES)
    rec.flush()
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.store import feedback_log_path
    rows = read_feedback(feedback_log_path())
    assert rows[0]["signal"] == "downgrade"
    assert rows[0]["weight"] == 0.3


# ---------- interrupt ----------

def test_interrupt_records_with_tier(rec):
    assert capture_interrupt(rec, "Superior") is True
    rec.flush()
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.store import feedback_log_path
    rows = read_feedback(feedback_log_path())
    assert rows[0]["source"] == "interrupt"
    assert rows[0]["model_tier"] == "Superior"


def test_interrupt_skipped_without_tier(rec):
    assert capture_interrupt(rec, None) is False
    rec.flush()
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.store import feedback_log_path
    assert read_feedback(feedback_log_path()) == []


# ---------- 累计多信号不丢失 ----------

def test_multiple_signals_same_turn(rec):
    emitted = capture_turn_signals(
        rec, "不对，重来一下", {"is_chatty": 1},
        prev_tier="Enhanced", cur_tier="Superior", TIER_NAMES=TIER_NAMES,
    )
    # clarify + cmd_retry（短+闲聊+高档还含 short_high_tier）
    assert len(emitted) >= 2
    rec.flush()
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.store import feedback_log_path
    assert len(read_feedback(feedback_log_path())) == len(emitted)