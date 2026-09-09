"""phase-04 A2 稳定层（会话内迟滞）测试。

覆盖：连续跳档冻结、硬规则解冻、窗口过期自动解冻、冻结压制 learned_rules、
与 600s 防降级叠加共存。
"""

from __future__ import annotations

from routivus.adaptive.learned_rules import LearnedRules
from routivus.router.postprocess import Hysteresis, postprocess


def _lr_upgrade_on_short() -> LearnedRules:
    """构造一条 len_chars<=60 → +1 的自学习规则。"""
    return LearnedRules.from_dict({"rules": [{
        "predicate": {"len_chars<=": 60.0},
        "action": 1, "confidence": 0.9, "support": 22,
    }]})


def _neutral_f() -> dict:
    """中性特征：命中短文本谓词，不命中任何硬规则词。"""
    return {"len_chars": 10, "num_code_blocks": 0,
            "num_chatty_kw": 0, "num_risk_kw": 0, "num_arch_kw": 0,
            "num_debug_kw": 0, "num_planning_kw": 0, "num_teach_kw": 0}


def test_step_initial_and_unchanged_no_count():
    h = Hysteresis()
    assert h.step(2, False, 100) == 2   # 无历史，直接采用
    assert h.step(2, False, 101) == 2   # 档位未变，不计数
    assert h.prev_tier == 2 and h.change_count == 0


def test_step_freezes_after_too_many_changes():
    h = Hysteresis()  # max_changes=1
    assert h.step(2, False, 100) == 2   # 无历史
    assert h.step(1, False, 102) == 1   # 变化 #1，允许
    # 变化 #2（>1）触发冻结，压回上一档 1
    assert h.step(0, False, 104) == 1
    assert h.frozen_tier == 1
    # 冻结期内再想变仍被压住
    assert h.step(0, False, 105) == 1


def test_step_hard_rule_unfreezes():
    h = Hysteresis()
    h.step(2, False, 100)
    h.step(1, False, 102)              # 变化 #1
    h.step(0, False, 104)              # 触发冻结，压回 1
    assert h.step(0, False, 106) == 1  # 冻结内
    assert h.step(3, True, 107) == 3   # 硬规则强制：解冻并放行
    assert h.frozen_tier is None


def test_step_window_expiry_auto_unfreeze():
    h = Hysteresis()
    h.step(2, False, 100)
    h.step(1, False, 102)              # 变化 #1
    h.step(0, False, 104)              # 触发冻结，压回 1（frozen_ts=104）
    # 超过 60s 窗口：重置计数并解冻，可再次变化
    assert h.step(0, False, 104 + 61) == 0
    assert h.frozen_tier is None


def test_postprocess_freeze_suppresses_learned_rules():
    h = Hysteresis()
    h.step(2, False, 100)
    h.step(1, False, 102)              # 变化 #1
    h.step(0, False, 104)              # 触发冻结，压回 1

    rules = _lr_upgrade_on_short()
    # 若不冻结：risk 无、防降级不挡、learned_rules +1 → t=3
    out = postprocess(2, "xyz wuv 1234", _neutral_f(),
                      prev_tier=1, prev_ts=102, ts=104,
                      learned_rules=rules, hysteresis=h)
    # 冻结是最后一道闸：learned_rules 的 +1 也被压回冻结档 1
    assert out == 1
    assert h.frozen_tier == 1


def test_postprocess_hard_rule_breaks_freeze():
    h = Hysteresis()
    h.step(2, False, 100)
    h.step(1, False, 102)
    h.step(0, False, 104)              # 冻结在 1

    # risk 硬规则输入：forced=True，突破冻结并强制 >= Superior(2)
    out = postprocess(2, "上线前需要做生产安全合规检查", _neutral_f(),
                      prev_tier=1, prev_ts=104, ts=106, hysteresis=h)
    assert out == 2
    assert h.frozen_tier is None


def test_postprocess_without_hysteresis_unchanged():
    """不传 hysteresis 时与既往行为一致（向后兼容）。"""
    rules = _lr_upgrade_on_short()
    out = postprocess(2, "xyz wuv 1234", _neutral_f(), learned_rules=rules)
    assert out == 3  # learned_rules +1，未加入迟滞