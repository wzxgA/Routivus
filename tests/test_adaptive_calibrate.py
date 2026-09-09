"""校准聚合与应用单测（phase-03 步骤 C）。

覆盖：样本不足跳过、±0.15/±0.1 夹紧边界、方向语义（upgrade 多 → bias 负）、
置信门（硬规则不动/置信足够不动/边界输入按偏置方向移动且最多一档）、
持久化 roundtrip 与损坏回退、recalibrate 无记录不写盘、route() 集成。
"""

from __future__ import annotations

import json

import pytest

from routivus.adaptive.calibrate import (
    MAX_BIAS,
    MAX_THRESHOLD_ADJUST,
    MIN_SAMPLES_PER_TIER,
    Calibration,
    aggregate,
    apply_calibration,
    load_calibration,
    recalibrate,
    save_calibration,
)
from routivus.adaptive.feedback import read_feedback
from routivus.router import route, TIER_NAMES


def _rec(tier: str, signal: str, weight: float = 1.0) -> dict:
    return {"ts": 0.0, "session": "t", "source": "clarify",
            "model_tier": tier, "signal": signal, "weight": weight}


@pytest.fixture()
def adapt_dir(monkeypatch, tmp_path):
    d = tmp_path / "adaptive"
    monkeypatch.setenv("XG_ADAPTIVE_DIR", str(d))
    return d


# ---------- aggregate ----------

def test_insufficient_samples_no_bias():
    cal = aggregate([_rec("Basic", "upgrade", 1.0)] * (MIN_SAMPLES_PER_TIER - 1))
    assert cal.bias[0] == 0.0
    assert cal.samples[0] == MIN_SAMPLES_PER_TIER - 1


def test_upgrade_heavy_tier_gets_negative_bias(adapt_dir):
    # 30 条 upgrade（weight 1.0）→ n=30 >= 20，r=+1 → bias=-0.15（夹紧下界）
    cal = aggregate([_rec("Basic", "upgrade", 1.0)] * 30)
    assert cal.bias[0] == pytest.approx(-MAX_BIAS)


def test_downgrade_heavy_tier_gets_positive_bias():
    cal = aggregate([_rec("Superior", "downgrade", 0.3)] * 100)
    # n=30（加权 0.3*100），r=-1 → bias=+0.15
    assert cal.samples[2] == pytest.approx(30.0)
    assert cal.bias[2] == pytest.approx(MAX_BIAS)


def test_bias_clamped_by_mixed_ratio():
    # r=0.2 → -r*0.15 = -0.03，未触夹紧
    up = [_rec("Enhanced", "upgrade", 1.0)] * 18
    down = [_rec("Enhanced", "downgrade", 1.0)] * 12
    cal = aggregate(up + down)
    assert cal.bias[1] == pytest.approx(-0.03)


def test_threshold_adjust_clamped():
    # 全部 upgrade → r_global=1 → clamp(+0.1)
    cal = aggregate([_rec("Basic", "upgrade", 1.0)] * 30)
    assert cal.threshold_adjust == pytest.approx(MAX_THRESHOLD_ADJUST)


def test_unknown_tier_and_signal_skipped():
    cal = aggregate([
        {"model_tier": "Unknown", "signal": "upgrade", "weight": 1.0},
        {"model_tier": "Basic", "signal": "weird", "weight": 1.0},
        {"model_tier": "Basic", "signal": "upgrade", "weight": 1.0},
    ])
    assert cal.total == 1.0  # 仅最后一条有效
    assert cal.bias[0] == 0.0  # 样本不足


def test_empty_records_empty_calibration():
    cal = aggregate([])
    assert cal.bias == (0.0, 0.0, 0.0, 0.0)
    assert cal.threshold_adjust == 0.0
    assert cal.total == 0.0


# ---------- apply_calibration ----------

def _cal(bias=(0.0, 0.0, 0.0, 0.0), adj=0.0):
    return Calibration(bias=bias, threshold_adjust=adj)


def test_hard_rule_never_adjusted():
    cal = _cal(bias=(-0.15, 0, 0, 0), adj=0.1)
    assert apply_calibration(0, confidence=1.0, hard_rule=True, calibration=cal) == 0


def test_zero_bias_no_move():
    cal = _cal()
    assert apply_calibration(2, confidence=0.5, hard_rule=False, calibration=cal) == 2


def test_high_confidence_not_gated():
    cal = _cal(bias=(-0.15, 0, 0, 0))
    # confidence + bias = 0.75 >= gate 0.5 → 保持
    assert apply_calibration(0, confidence=0.9, hard_rule=False, calibration=cal) == 0


def test_low_confidence_negative_bias_upgrades_one_tier():
    # confidence + bias = 0.5 - 0.15 = 0.35 < gate 0.5 → 升一档
    cal = _cal(bias=(-0.15, 0, 0, 0))
    assert apply_calibration(0, confidence=0.5, hard_rule=False, calibration=cal) == 1


def test_low_confidence_positive_bias_downgrades_one_tier():
    # confidence - bias = 0.5 - 0.15 = 0.35 < gate 0.5 → 降一档（对称门）
    cal = _cal(bias=(0, 0, 0.15, 0))
    assert apply_calibration(2, confidence=0.5, hard_rule=False, calibration=cal) == 1


def test_adjustment_clamped_at_tier_bounds():
    # bias>0 在 Ultimate(3) 降一档可行；bias<0 在 Basic(0) 升一档可行
    assert apply_calibration(3, confidence=0.5, hard_rule=False,
                            calibration=_cal(bias=(0, 0, 0, 0.15))) == 2
    assert apply_calibration(0, confidence=0.5, hard_rule=False,
                            calibration=_cal(bias=(-0.15, 0, 0, 0))) == 1


def test_threshold_adjust_shifts_gate():
    # adj=+0.1 → gate=0.6：confidence+bias=0.55+(-0.15)=0.4 < 0.6 → 升档
    cal = _cal(bias=(-0.15, 0, 0, 0), adj=0.1)
    assert apply_calibration(0, confidence=0.55, hard_rule=False, calibration=cal) == 1
    # adj=-0.1 → gate=0.4：同样的输入不再过门（校准更保守）
    cal2 = _cal(bias=(-0.15, 0, 0, 0), adj=-0.1)
    assert apply_calibration(0, confidence=0.55, hard_rule=False, calibration=cal2) == 0


# ---------- 持久化 ----------

def test_save_load_roundtrip(adapt_dir):
    cal = Calibration(bias=(-0.15, 0.0, 0.03, 0.1), threshold_adjust=-0.05,
                      samples=(30.0, 0.0, 25.0, 20.0), total=75.0)
    save_calibration(cal)
    loaded = load_calibration()
    assert loaded.bias == cal.bias
    assert loaded.threshold_adjust == cal.threshold_adjust
    assert loaded.samples == cal.samples
    assert loaded.total == cal.total


def test_load_corrupt_falls_back_to_empty(adapt_dir):
    adapt_dir.mkdir(parents=True, exist_ok=True)
    (adapt_dir / "calibration.json").write_text("{broken json", encoding="utf-8")
    cal = load_calibration()
    assert cal.bias == (0.0, 0.0, 0.0, 0.0)


def test_load_missing_returns_empty(adapt_dir):
    assert load_calibration().total == 0.0


def test_recalibrate_no_records_writes_nothing(adapt_dir):
    cal = recalibrate()
    assert cal.total == 0.0
    assert not adapt_dir.exists()  # 不重建目录，删掉即回第 1 期行为


def test_recalibrate_aggregates_and_persists(adapt_dir):
    from routivus.adaptive.feedback import FeedbackRecorder, SignalType

    rec = FeedbackRecorder(session="t")
    for _ in range(30):
        rec.capture(SignalType.CLARIFY, model_tier="Basic", text="不对")
    rec.flush()
    assert len(read_feedback()) == 30

    cal = recalibrate()
    assert cal.bias[0] == pytest.approx(-MAX_BIAS)
    data = json.loads((adapt_dir / "calibration.json").read_text(encoding="utf-8"))
    assert data["bias"]["Basic"] == pytest.approx(-MAX_BIAS)


# ---------- route() 集成：校准只读注入 ----------

def test_route_without_calibration_unchanged():
    """不传 calibration（或传 None）= 第 1 期纯规则行为。"""
    r1 = route("你好", fallback_provider="openai", fallback_model="m")
    assert r1.tier == "Basic"


def _boundary_tier_text(tier_idx: int) -> str:
    """构造落在指定档边界附近（低置信度）的软规则输入。"""
    # Enhanced 边界：得分略高于 1.0 的实现类请求
    if tier_idx == 1:
        return "实现一个工具函数"  # 低分软规则 → Enhanced 附近
    raise NotImplementedError


def test_route_applies_calibration_on_soft_rule():
    # 无校准：Enhanced 边界输入 → Enhanced（score=2.0, confidence≈0.625）
    text = _boundary_tier_text(1)
    base = route(text, fallback_provider="openai", fallback_model="m")
    assert base.tier == "Enhanced"
    assert base.hard_rule is False

    # Enhanced 偏弱（upgrade 信号多 → bias 负）：conf+bias=0.625-0.15=0.475 < 0.5 → 升一档
    cal = _cal(bias=(0, -0.15, 0, 0))
    adjusted = route(text, fallback_provider="openai", fallback_model="m", calibration=cal)
    assert adjusted.tier == "Superior"
    # 幅度受限：只升一档，不会跳到 Ultimate
    assert TIER_NAMES.index(adjusted.tier) - TIER_NAMES.index(base.tier) == 1


def test_route_hard_rule_immune_to_calibration():
    # 风险词触发硬规则（rule_route 层），postprocess 再升到 Ultimate
    text = "设计生产环境数据库迁移回滚方案"
    base = route(text, fallback_provider="openai", fallback_model="m")
    assert base.hard_rule is True

    # Superior/Ultimate 偏强也不受校准影响（安全兜底优先）
    cal = _cal(bias=(0, 0, 0.15, 0.15))
    adjusted = route(text, fallback_provider="openai", fallback_model="m", calibration=cal)
    assert adjusted.tier == base.tier
