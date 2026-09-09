"""adaptive 反馈骨架单测。"""

from __future__ import annotations

import json

import pytest

from routivus.adaptive.feedback import (
    FeedbackRecorder,
    SignalType,
    read_feedback,
    text_hash,
)


@pytest.fixture()
def adapt_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))
    return tmp_path


def test_signal_types_include_reserved_file_revert():
    assert SignalType.FILE_REVERT.value == "file_revert"


def test_signal_meta_weights():
    assert SignalType.CLARIFY.value in ("clarify",)
    # 四项可采集 + 一项预留
    assert len(SignalType) == 5


def test_text_hash_stable_and_short():
    a = text_hash("写个二分查找")
    b = text_hash("写个二分查找")
    assert a == b
    assert len(a) == 12


def test_capture_buffers_not_flushed(adapt_dir):
    rec = FeedbackRecorder(session="proj-x")
    rec.capture(SignalType.CLARIFY, model_tier="Superior", text="不对，再改一下")
    assert rec.count() == 1
    # 未 flush 前 feedback.log 不存在
    assert not (adapt_dir / "feedback.log").exists()


def test_flush_empty_is_noop(adapt_dir):
    rec = FeedbackRecorder(session="proj-x")
    assert rec.flush() == 0
    assert not (adapt_dir / "feedback.log").exists()


def test_flush_writes_jsonl_and_clears(adapt_dir):
    rec = FeedbackRecorder(session="proj-x")
    rec.capture(SignalType.CLARIFY, model_tier="Superior", text="hi")
    assert rec.flush() == 1
    assert rec.count() == 0
    lines = (adapt_dir / "feedback.log").read_text(encoding="utf-8").strip().splitlines()
    rec2 = json.loads(lines[0])
    assert rec2["source"] == "clarify"
    assert rec2["model_tier"] == "Superior"
    assert rec2["session"] == "proj-x"
    assert rec2["signal"] == "upgrade"
    assert rec2["weight"] == 1.0
    assert rec2["text_hash"]


def test_short_high_tier_is_downgrade_signal(adapt_dir):
    rec = FeedbackRecorder(session="proj-x")
    rec.capture(SignalType.SHORT_HIGH_TIER, model_tier="Superior", text="你好")
    rec.flush()
    recs = read_feedback(adapt_dir / "feedback.log")
    assert recs[0]["signal"] == "downgrade"
    assert recs[0]["weight"] == 0.3


def test_read_feedback_missing_returns_empty(adapt_dir):
    assert read_feedback(adapt_dir / "absent.log") == []


def test_read_feedback_skips_corrupt_line(adapt_dir):
    p = adapt_dir / "feedback.log"
    p.write_text("{bad json}\n{\"ts\":1, \"source\":\"interrupt\"}\n", encoding="utf-8")
    recs = read_feedback(p)
    assert len(recs) == 1
    assert recs[0]["source"] == "interrupt"


def test_flush_appends_across_calls(adapt_dir):
    rec = FeedbackRecorder(session="s")
    rec.capture(SignalType.INTERRUPT, model_tier="Basic")
    rec.flush()
    rec.capture(SignalType.CLARIFY, model_tier="Basic")
    rec.flush()
    assert len(read_feedback(adapt_dir / "feedback.log")) == 2