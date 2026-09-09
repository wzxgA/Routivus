"""phase-04 A3（reset 与观测）测试。

覆盖：reset 后回到无校准无规则状态、rule_hit_stats 命中统计、
route() 透传 hysteresis（迟滞在整条路由链路生效）。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from routivus.adaptive.calibrate import (aggregate, load_calibration, recalibrate,
                                   save_calibration)
from routivus.adaptive.learned_rules import (LearnedRules, load_learned_rules,
                                       re_learn, rule_hit_stats)
from routivus.adaptive.store import reset_adaptive_data
from routivus.router import route
from routivus.router.postprocess import Hysteresis


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")


def _signal(weight: float = 1.0, features=None) -> dict:
    return {
        "ts": 1000.0, "session": "s", "source": "clarify",
        "signal": "upgrade", "text_hash": "abc", "model_tier": "Basic",
        "weight": weight, "features": features,
    }


def test_reset_adaptive_data_removes_both(monkeypatch, tmp_path):
    log = tmp_path / "feedback.log"
    cal_path = tmp_path / "calibration.json"
    rules_path = tmp_path / "learned_rules.json"
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))

    # 先造出两个文件
    records = [_signal(w, {"len_chars": 10, "num_code_blocks": 0}) for w in [1.0]] * 22
    _write_jsonl(log, records)
    cal = recalibrate(log, cal_path)
    assert cal.samples[0] == 22.0
    rules = re_learn(log, rules_path)
    assert rules.count >= 1
    assert cal_path.exists() and rules_path.exists()

    removed = reset_adaptive_data()
    assert set(removed) == {"calibration.json", "learned_rules.json"}
    assert not cal_path.exists() and not rules_path.exists()
    assert log.exists()  # feedback.log 保留作历史

    # 回到无校准、无规则状态
    assert aggregate([]).samples[0] == 0.0
    assert load_calibration(cal_path).bias[0] == 0.0
    assert load_learned_rules(rules_path).count == 0


def test_reset_adaptive_data_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))
    assert reset_adaptive_data() == []


def test_rule_hit_stats():
    rules = LearnedRules.from_dict({"rules": [{
        "predicate": {"len_chars<=": 60.0},
        "action": 1, "confidence": 0.9, "support": 22,
    }]})
    records = [
        _signal(features={"len_chars": 10, "num_code_blocks": 0}),
        _signal(features={"len_chars": 20, "num_code_blocks": 0}),
        _signal(features={"len_chars": 999, "num_code_blocks": 0}),  # 未命中
        _signal()                                                     # 无 features，跳过
    ]
    stats = rule_hit_stats(records, rules)
    assert stats["rule_count"] == 1
    assert stats["sample_records"] == 3
    assert stats["hit_records"] == 2
    assert stats["per_rule"][0]["hits"] == 2


def test_route_forwards_hysteresis():
    """整条 route() 链路透传 hysteresis：冻结档压制本该跳高的输入。"""
    h = Hysteresis()
    # 冻结到档位 1（Enhanced）：两次变化后触发迟滞冻结
    h.step(2, False, 100)
    h.step(1, False, 102)              # 变化 #1
    h.step(0, False, 104)              # 变化 #2 → 冻结，压回 1

    # 架构文本裸路由会跳高档，但迟滞冻结作为最后一道闸压回 1
    out = route("给我做一套系统架构设计",
                prev_tier=1, prev_ts=102, ts=104, hysteresis=h)
    assert out.tier_idx == 1
    assert h.frozen_tier == 1