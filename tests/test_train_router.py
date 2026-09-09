"""第 5 期 B1（离线训练脚本）测试。

纯逻辑部分（tier 解析 / 反推标签 / 聚合去噪 / 样本构建）无 ML 依赖直接测；
训练端到端部分缺 ML 依赖时整段 skip（与产物"缺依赖静默回落"语义一致）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from train_router import (aggregate_by_hash, build_samples, derive_label,  # noqa: E402
                          tier_to_idx, train_and_save)


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------

class TestTierToIdx:
    def test_names(self):
        assert tier_to_idx("Basic") == 0
        assert tier_to_idx("Ultimate") == 3

    def test_int_passthrough(self):
        assert tier_to_idx(2) == 2
        assert tier_to_idx(9) is None
        assert tier_to_idx(-1) is None

    def test_unknown(self):
        assert tier_to_idx("basic") is None  # 大小写敏感，与 TIER_NAMES 一致
        assert tier_to_idx(None) is None


class TestDeriveLabel:
    def test_upgrade(self):
        assert derive_label(0, "upgrade") == 1
        assert derive_label(2, "upgrade") == 3

    def test_downgrade(self):
        assert derive_label(3, "downgrade") == 2
        assert derive_label(1, "downgrade") == 0

    def test_boundary_dropped(self):
        # 到顶 upgrade / 到底 downgrade：无区分度 → 丢弃
        assert derive_label(3, "upgrade") is None
        assert derive_label(0, "downgrade") is None

    def test_unknown_direction(self):
        assert derive_label(1, "") is None
        assert derive_label(1, "sideway") is None


class TestAggregateByHash:
    def _rec(self, h, signal, weight=1.0, tier="Basic"):
        return {"text_hash": h, "signal": signal, "weight": weight,
                "model_tier": tier, "features": {}}

    def test_majority_direction_wins(self):
        recs = [self._rec("a", "upgrade", 1.0),
                self._rec("a", "upgrade", 0.6),
                self._rec("a", "downgrade", 0.3)]
        out = aggregate_by_hash(recs)
        assert len(out) == 1 and out[0]["signal"] == "upgrade"
        assert out[0]["weight"] == pytest.approx(1.6)

    def test_tie_dropped(self):
        recs = [self._rec("a", "upgrade", 0.6),
                self._rec("a", "downgrade", 0.6)]
        assert aggregate_by_hash(recs) == []

    def test_no_hash_passthrough(self):
        recs = [self._rec("", "upgrade")]
        out = aggregate_by_hash(recs)
        assert len(out) == 1 and out[0]["text_hash"] == ""


class TestBuildSamples:
    def test_labeled_only(self, tmp_path):
        lp = tmp_path / "labeled.jsonl"
        lp.write_text(
            json.dumps({"text": "帮我部署生产架构", "tier": "Superior"}) + "\n" +
            json.dumps({"text": "你好", "tier": 0}) + "\n",
            encoding="utf-8",
        )
        samples, stats = build_samples(lp, [])
        assert stats["labeled"] == 2 and stats["feedback"] == 0
        assert samples[0]["tier"] == 2 and samples[0]["weight"] == 1.0
        assert samples[1]["tier"] == 0

    def test_labeled_invalid_lines_dropped(self, tmp_path):
        lp = tmp_path / "bad.jsonl"
        lp.write_text(
            json.dumps({"text": "x", "tier": "Nope"}) + "\n" +
            json.dumps({"text": "", "tier": 1}) + "\n" +
            "not-json\n", encoding="utf-8")
        samples, stats = build_samples(lp, [])
        assert samples == [] and stats["dropped_no_label"] == 3

    def test_feedback_label_derived(self):
        recs = [{"text_hash": "h1", "signal": "upgrade", "weight": 1.0,
                 "model_tier": "Basic",
                 "features": {"len_chars": 10, "num_risk_kw": 0, "is_chatty": 0}}]
        samples, stats = build_samples(None, recs)
        assert stats["feedback"] == 1
        assert samples[0]["tier"] == 1  # Basic + upgrade → Enhanced
        assert samples[0]["text"] == ""  # 无原文，TF-IDF 列将全零
        assert samples[0]["weight"] == 1.0

    def test_feedback_hard_rule_dropped(self):
        recs = [{"text_hash": "h1", "signal": "upgrade", "weight": 1.0,
                 "model_tier": "Basic",
                 "features": {"num_risk_kw": 1}},
                {"text_hash": "h2", "signal": "downgrade", "weight": 0.3,
                 "model_tier": "Superior",
                 "features": {"is_chatty": 1}}]
        samples, stats = build_samples(None, recs)
        assert samples == [] and stats["dropped_hard_rule"] == 2

    def test_feedback_boundary_dropped(self):
        recs = [{"text_hash": "h1", "signal": "upgrade", "weight": 1.0,
                 "model_tier": "Ultimate", "features": {}}]
        samples, stats = build_samples(None, recs)
        assert samples == [] and stats["dropped_no_label"] == 1

    def test_conflicting_hash_aggregated(self):
        recs = [{"text_hash": "h", "signal": "upgrade", "weight": 0.6,
                 "model_tier": "Enhanced", "features": {}},
                {"text_hash": "h", "signal": "downgrade", "weight": 0.3,
                 "model_tier": "Enhanced", "features": {}}]
        samples, _ = build_samples(None, recs)
        # 加权投票 upgrade(0.6) > downgrade(0.3) → 单条样本 Enhanced+1
        assert len(samples) == 1 and samples[0]["tier"] == 2

    def test_empty(self, tmp_path):
        samples, stats = build_samples(None, [])
        assert samples == [] and stats["labeled"] == 0


# ---------------------------------------------------------------------------
# 训练端到端（缺 ML 依赖整类 skip）
# ---------------------------------------------------------------------------

def _ml_available() -> bool:
    try:
        import joblib  # noqa: F401
        import lightgbm  # noqa: F401
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestTrainAndSave:
    def _samples(self, n=40):
        # 四档各 n/4 条，文本带可判别的模式词
        texts = {
            0: ["你好呀随便聊聊%s" % i for i in range(n // 4)],
            1: ["写个函数把两个数相加第%d条" % i for i in range(n // 4)],
            2: ["帮我排查这个模块的性能问题 %d" % i for i in range(n // 4)],
            3: ["设计生产环境分布式部署架构方案 %d" % i for i in range(n // 4)],
        }
        out = []
        for tier, ts in texts.items():
            out.extend({"text": t, "tier": tier, "weight": 1.0, "features": None}
                       for t in ts)
        return out

    def test_train_produce_artifact(self, tmp_path):
        out = tmp_path / "router.lgb"
        report = train_and_save(self._samples(40), out)
        assert out.exists() and out.stat().st_size > 0
        assert report["n_samples"] == 40
        assert report["val_accuracy"] is not None  # 有验证集

        import joblib
        payload = joblib.load(out)
        assert payload["format"] == 1
        assert payload["n_samples"] == 40
        assert payload["vectorizer"] is not None
        assert hasattr(payload["model"], "predict")

    def test_predict_new_text(self, tmp_path):
        # 训出的模型对新模式文本应能给出合法档位
        out = tmp_path / "router.lgb"
        train_and_save(self._samples(60), out)
        import joblib
        import numpy as np
        from scipy.sparse import csr_matrix, hstack
        payload = joblib.load(out)
        feats = np.zeros((1, len(payload["feature_keys"])))
        x = hstack([payload["vectorizer"].transform(
            ["设计生产环境分布式部署架构方案"]), csr_matrix(feats)]).tocsr()
        pred = payload["model"].predict(x)
        assert pred[0] in (0, 1, 2, 3)

    def test_few_samples_no_val_split(self, tmp_path):
        out = tmp_path / "router.lgb"
        report = train_and_save(self._samples(8), out)
        assert report["n_samples"] == 8
        assert report["val_accuracy"] is None  # 样本少不切验证
        assert out.exists()

    def test_feedback_only_no_text(self, tmp_path):
        # 纯 feedback 训练：无原文（TF-IDF 零宽），仅数值特征，不崩
        samples = [
            {"text": "", "tier": 1, "weight": 1.0,
             "features": {"len_chars": 10, "num_debug_kw": 1, "num_risk_kw": 0}},
        ] * 30
        out = tmp_path / "router.lgb"
        report = train_and_save(samples, out)
        assert out.exists()
        assert report["n_samples"] == 30
        import joblib
        payload = joblib.load(out)
        assert payload["vectorizer"] is None  # 无文本 → vectorizer 缺席
