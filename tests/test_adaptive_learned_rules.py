"""learned_rules 聚合与消费测试。

覆盖：
- 候选谓词生成（_candidates）
- 聚合去重、生成格式、方向判定
- 样本不足 / 弱偏向不生成
- ±1 档限制、硬规则不可被 learned_rules 覆盖
- 旧格式记录（无 features）跳过（向后兼容）
- 无记录不写盘；删除 learned_rules.json 回到空态
- save/load roundtrip 与损坏回退
"""

from __future__ import annotations

import pytest

from routivus.adaptive.learned_rules import (
    LearnedRule,
    LearnedRules,
    MAX_CONFIDENCE,
    aggregate,
    load_learned_rules,
    re_learn,
)
from routivus.router.features import extract
from routivus.router.postprocess import postprocess

# 只命中 num_debug_kw 单谓词（len_chars>60，避免 len<=60 谓词干扰）
_DEBUG_ONLY = {"num_debug_kw": 1, "len_chars": 100, "num_code_blocks": 0}
# 只命中 is_chatty 单谓词
_CHATTY_ONLY = {"is_chatty": 1, "len_chars": 100, "num_code_blocks": 0}


def _up(features) -> dict:
    return {"signal": "upgrade", "weight": 1.0, "model_tier": "Enhanced", "features": features}


def _down(features) -> dict:
    return {"signal": "downgrade", "weight": 1.0, "model_tier": "Enhanced", "features": features}


# ---------- 候选谓词生成 ----------

def test_candidates_from_features():
    from routivus.adaptive.learned_rules import _candidates

    preds = _candidates({"num_debug_kw": 1, "len_chars": 12})
    assert ("num_debug_kw", ">=", 1.0) in preds
    assert ("len_chars", "<=", 60.0) in preds

    assert _candidates({"num_code_blocks": 0, "len_chars": 100}) == []


# ---------- 聚合 ----------

def test_aggregate_builds_rule_from_majority_upgrade():
    records = [_up(_DEBUG_ONLY) for _ in range(30)]  # 30 条 upgrade
    rules = aggregate(records)
    assert rules.count == 1

    r = rules.rules[0]
    assert r.feature == "num_debug_kw"
    assert r.action == 1            # upgrade 多 → 升规则
    assert r.support >= 20
    assert r.confidence <= MAX_CONFIDENCE


def test_aggregate_downgrade_action():
    records = [_down(_DEBUG_ONLY) for _ in range(30)]  # 30 条 downgrade
    rules = aggregate(records)
    assert rules.count == 1
    assert rules.rules[0].action == -1


def test_aggregate_skips_below_min_support():
    records = [_up(_DEBUG_ONLY) for _ in range(19)]  # 差一条到 20
    assert aggregate(records).count == 0


def test_aggregate_skips_weak_majority():
    # 17 vs 13 → 占比 0.567 < 0.6，不生成
    records = [_up(_DEBUG_ONLY) for _ in range(17)] + \
              [_down(_DEBUG_ONLY) for _ in range(13)]
    assert aggregate(records).count == 0


def test_aggregate_skips_old_format_without_features():
    # 旧记录无 features 字段 → 全被跳过
    records = [{"signal": "upgrade", "weight": 1.0, "model_tier": "Enhanced"} for _ in range(30)]
    assert aggregate(records).count == 0


def test_aggregate_merges_duplicate_predicates_takes_latest():
    # 同一谓词 num_debug_kw 累积 upgrade 与 downgrade，取多数派方向
    records = [_up(_DEBUG_ONLY) for _ in range(22)] + \
              [_down(_DEBUG_ONLY) for _ in range(8)]
    rules = aggregate(records)
    assert rules.count == 1
    assert rules.rules[0].action == 1


def test_aggregate_sort_by_support_desc():
    records = [_up(_CHATTY_ONLY) for _ in range(30)] + \
              [_up(_DEBUG_ONLY) for _ in range(22)]
    rules = aggregate(records)
    assert rules.count == 2
    supports = [r.support for r in rules.rules]
    assert supports == sorted(supports, reverse=True)


# ---------- LearnedRules.apply（±1 限制、排序取首条） ----------

def test_apply_bumper():
    f = extract("实现一个红包算法")
    r1 = LearnedRule("num_impl_kw", ">=", 1.0, action=1, confidence=0.9, support=30)
    rules = LearnedRules((r1,))
    assert rules.apply(f) == 1


def test_apply_no_match():
    f = extract("你好")  # 无 impl 词
    r1 = LearnedRule("num_impl_kw", ">=", 1.0, action=1, confidence=0.9, support=30)
    assert LearnedRules((r1,)).apply(f) == 0


def test_apply_takes_first_by_support_only():
    f = {"num_debug_kw": 1, "len_chars": 12}  # 同时命中两条规则
    up = LearnedRule("num_debug_kw", ">=", 1.0, action=1, confidence=0.9, support=20)
    down = LearnedRule("len_chars", "<=", 60.0, action=-1, confidence=0.9, support=30)
    rules = LearnedRules((down, up))  # support 降序已排好
    assert rules.apply(f) == -1  # 优先取高 support 的 down


# ---------- postprocess 集成：±1 与硬规则保护 ----------

def test_postprocess_applies_learned_rules_in_bound():
    f = {"len_chars": 12, "num_code_blocks": 0}
    rules = LearnedRules((LearnedRule("len_chars", "<=", 60.0, action=1,
                                       confidence=0.9, support=30),))
    # 文本无内置 debug/risk/chatty 词 → t=1，规则 +1 → 2
    assert postprocess(1, "xyz wuv 1234", f, learned_rules=rules) == 2
    # 边界不越档：从 Ultimate=3 升 → 仍 3
    assert postprocess(3, "xyz wuv 1234", f, learned_rules=rules) == 3


def test_postprocess_rise_rules_not_overridden_by_hard_rule():
    """风险硬规则优先于 learned_rules：即使规则想降档，硬规则仍锁定。"""
    text = "准备生产环境部署方案"  # 含 risk（生产/部署），不含 debug 词
    f = extract(text)
    rules = LearnedRules((LearnedRule("len_chars", "<=", 60.0, action=-1,
                                       confidence=0.9, support=30),))
    # risk → 强制 >= Superior(2)；learned_rules -1 不得推翻
    assert postprocess(2, text, f, learned_rules=rules) == 2


def test_postprocess_learned_rules_skipped_on_chatty():
    f = {"is_chatty": 1, "num_code_blocks": 0, "len_chars": 5}
    rules = LearnedRules((LearnedRule("is_chatty", ">=", 1.0, action=1,
                                       confidence=0.9, support=30),))
    # 闲聊硬规则强制 Basic(0)，learned_rules 想升档不可推翻
    assert postprocess(0, "你好", f, learned_rules=rules) == 0


# ---------- 持久化 ----------

def test_relearn_no_records_does_not_write(tmp_path, monkeypatch):
    d = tmp_path / "adaptive"
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(d))
    rules = re_learn()
    assert rules.count == 0
    assert not d.exists()  # 不重建目录（回到空态）


def test_save_load_roundtrip():
    rules = LearnedRules((LearnedRule("num_debug_kw", ">=", 1.0, action=1,
                                       confidence=0.9, support=30),))
    data = rules.to_dict()
    restored = LearnedRules.from_dict(data)
    assert restored.count == 1
    assert restored.rules[0].feature == "num_debug_kw"
    assert restored.rules[0].action == 1


def test_load_corrupt_falls_back_empty():
    assert LearnedRules.from_dict(None).count == 0
    assert LearnedRules.from_dict({"bogus": 1}).count == 0


def test_load_from_disk_empty_after_delete(tmp_path, monkeypatch):
    d = tmp_path / "adaptive"
    d.mkdir()
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(d))
    # 无 learned_rules.json → 空规则集，不抛错
    assert load_learned_rules().count == 0


def test_relearn_writes_and_loads(tmp_path, monkeypatch):
    d = tmp_path / "adaptive"
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(d))
    log = d / "feedback.log"
    import json

    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        for _ in range(30):
            fh.write(json.dumps(_up(_DEBUG_ONLY)) + "\n")
    rules = re_learn(log_path=log)
    assert rules.count == 1
    assert (d / "learned_rules.json").exists()
    assert load_learned_rules().count == 1