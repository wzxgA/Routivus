"""phase-03 步骤 D：集成与验收测试。

覆盖文档 §D + §8 验收标准：
- 集成链路：连续追问累积信号 → 聚合 → 同类输入档位上调一档；
- flush 时序：缓冲期内（未 flush）零磁盘写，flush 后落盘；
- off 开关：零采集零写盘（patch 断言 `_route_user_turn` 守卫）；
- 启动冒烟：加载三文件（feedback/calibration）空目录即空结果，不抛错。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from routivus.adaptive.calibrate import MAX_BIAS, Calibration, recalibrate
from routivus.adaptive.feedback import FeedbackRecorder, SignalType
from routivus.router import TIER_NAMES, route


# ---------- 集成链路：连续追问 → 同类输入档位上调一档 ----------

@pytest.fixture()
def adapt_dir(monkeypatch, tmp_path):
    d = tmp_path / "adaptive"
    monkeypatch.setenv("XG_ADAPTIVE_DIR", str(d))
    return d


def _enhanced_boundary_input() -> str:
    """构造落在 Enhanced 边界（score=1.0，confidence≈0.5）的输入。

    "写" 为实现词 +1 分 → score=1.0，>= 1.0（不落 Basic）且 <= 5.0 → Enhanced。
    """
    return "写个函数把两个数相加"


def test_repetitive_clarify_upgrades_boundary_input(adapt_dir):
    """文档 §D：连续追问 → 同类输入档位上调一档（端到端链路）。"""
    # 1) 用户反复在 Enhanced 档回答后追问（clarify），累积 20+ 条满足门槛
    rec = FeedbackRecorder(session="t", log_path=adapt_dir / "feedback.log")
    for _ in range(30):
        rec.capture(SignalType.CLARIFY, model_tier="Enhanced", text="不对")
    rec.flush()

    # 2) 启动时聚合 → 落盘 calibration.json
    cal = recalibrate()

    # Enhanced 30 条 upgrade → r=1 → bias 夹紧到 -0.15
    assert cal.bias[1] == pytest.approx(-MAX_BIAS)

    # 3) 同一天输入同类边界问题，走 route（带校准）
    base = route(_enhanced_boundary_input(),
                 fallback_provider="openai", fallback_model="m")
    assert base.tier == "Enhanced"
    assert base.hard_rule is False  # 软规则，才可被校准移动
    assert base.confidence < 0.6

    adjusted = route(_enhanced_boundary_input(),
                     fallback_provider="openai", fallback_model="m",
                     calibration=cal)
    # 同类输入：Enhanced → Superior，只上调一档
    assert adjusted.tier == "Superior"
    assert TIER_NAMES.index(adjusted.tier) - TIER_NAMES.index(base.tier) == 1


def test_calibration_never_jumps_more_than_one_tier(adapt_dir):
    """即使样本极端一边倒，校准也最多移动一档（不跳档）。"""
    rec = FeedbackRecorder(session="t", log_path=adapt_dir / "feedback.log")
    for _ in range(30):
        rec.capture(SignalType.CLARIFY, model_tier="Enhanced", text="不对")
    rec.flush()
    cal = recalibrate()

    base = route(_enhanced_boundary_input(),
                 fallback_provider="openai", fallback_model="m")
    adjusted = route(_enhanced_boundary_input(),
                     fallback_provider="openai", fallback_model="m",
                     calibration=cal)
    # 幅度受限：bias 夹紧 -0.15 + 单次一档，Enhanced 最多到 Superior，绝不跳 Ultimate
    delta = TIER_NAMES.index(adjusted.tier) - TIER_NAMES.index(base.tier)
    assert 0 < delta <= 1


# ---------- flush 时序：缓冲期内零写盘 ----------

def test_no_disk_write_until_flush(adapt_dir):
    """capture 只进内存缓冲；未 flush 前 feedback.log 不存在/不变化。"""
    log = adapt_dir / "feedback.log"
    rec = FeedbackRecorder(session="t", log_path=log)

    rec.capture(SignalType.CLARIFY, model_tier="Enhanced", text="不对")
    assert not log.exists()  # 缓冲期内无磁盘写
    assert rec.count() == 1

    rec.flush()
    assert log.exists()  # flush 后落盘
    assert rec.count() == 0
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_second_round_buffered_until_flush_again(adapt_dir):
    """连续多轮：每次 capture 后未 flush 都不写盘，flush 才追加。"""
    log = adapt_dir / "feedback.log"
    rec = FeedbackRecorder(session="t", log_path=log)

    rec.capture(SignalType.CLARIFY, model_tier="Basic", text="再改")
    rec.flush()
    rec.capture(SignalType.CMD_RETRY, model_tier="Basic", text="重来")
    # 第二轮捕获后尚未 flush → 磁盘仍是第一轮 1 行
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 1
    rec.flush()
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2


# ---------- off 开关：零采集零写盘（patch 断言守卫） ----------

def test_route_user_turn_off_never_collects_or_touches_disk():
    """`_route_user_turn` 在开关关闭时立即返回，不路由、不采集、不切模型。"""
    import routivus.tui.controller as ctrl

    fake = object.__new__(ctrl.SessionController)
    fake.settings = type("S", (), {"smart_router_enabled": False})()

    with patch.object(ctrl, "route_turn") as m_route, \
            patch.object(ctrl, "capture_turn_signals") as m_cap, \
            patch.object(ctrl.SessionController, "_sync_smart_router_snapshot") as m_sync:
        fake._route_user_turn("写个函数把两个数相加")

        m_route.assert_not_called()
        m_cap.assert_not_called()
        m_sync.assert_not_called()


# ---------- 启动冒烟：三文件加载，空目录即空结果 ----------

def test_startup_loads_empty_state_cleanly(adapt_dir):
    """启动路径：空的 adaptive 目录 → feedback 空、calibration 空、不写盘。"""
    from routivus.adaptive.feedback import read_feedback
    from routivus.adaptive.calibrate import load_calibration

    assert read_feedback() == []          # 无 feedback.log → 空记录
    assert recalibrate().total == 0.0     # 无记录 → 空校准，不写盘
    assert not adapt_dir.exists()         # 不重建目录（回第 1 期行为）
    # 启动一次后：无记录仍未生成 calibration.json（load 回退空）
    assert load_calibration().total == 0.0
    assert not (adapt_dir / "calibration.json").exists()