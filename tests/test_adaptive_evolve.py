"""方案 10：出厂兜底 + 本地进化的单测。

覆盖（对齐方案 §7 的验收表）：

- 回落链：语义产物需要编码器 → 编码器不可用时用无语义兜底产物；都没有 → 不可用 + 原因码
- 本地样本库：语义向量 int8 往返；**文件里没有 text 字段**（隐私硬要求）
- 进化门槛：档位/每档/总量/冷却/增量 五道门槛各自触发 skip
- **不劣门**：候选更差时拒绝替换，产物保持原样
- 替换路径：接受时备份 .prev、原子落位、状态与 evolve.log 落盘
- reset --hard：连本地产物一起清掉，回到出厂基线
- L3 语义头：样本不足不训练；足够时产出 4 维概率列
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routivus.adaptive import evolve, samples as samples_mod, semantic_head
from routivus.adaptive.store import (
    calibration_path,
    evolve_log_path,
    evolve_state_path,
    learned_rules_path,
    ml_router_path,
    ml_router_prev_path,
    reset_adaptive_data,
    sem_samples_path,
)
from routivus.adaptive.training import FEATURE_KEYS, artifact_accuracy, load_artifact
from routivus.router.ml_router import MLRouter
from routivus.router.semantic import SemanticEncoder


def _ml_available() -> bool:
    try:
        import lightgbm  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture
def adaptive_dir(tmp_path, monkeypatch):
    """把 adaptive 数据目录指到 tmp（data_dir 每次调用都读 env）。"""
    target = tmp_path / "adaptive"
    target.mkdir()
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(target))
    return target


# ---------------------------------------------------------------------------
# 回落链与原因码（批次 1）
# ---------------------------------------------------------------------------

class _NoEncoder:
    """不可用的语义编码器替身。"""

    dim = 512
    available = False
    unavailable_reason = "runtime_missing"

    def encode(self, text: str):  # noqa: ARG002
        return None


class _StubEncoder:
    """可用的语义编码器替身（全零向量，够走通分支）。"""

    dim = 512
    available = True
    unavailable_reason = ""

    def encode(self, text: str):  # noqa: ARG002
        return [0.0] * 512


try:  # sklearn 缺失时退化为普通类（用它的用例本来就带 skipif）
    from sklearn.base import BaseEstimator as _BaseEstimator
except ImportError:  # pragma: no cover
    _BaseEstimator = object  # type: ignore[assignment,misc]


class _StubModel(_BaseEstimator):  # type: ignore[misc,valid-type]
    """最小分类器替身。必须模块级（joblib 无法 pickle 函数内定义的类），
    且必须是 BaseEstimator 子类 —— ``check_is_fitted`` 会先校验"是不是估计器"。"""

    def __init__(self, tier: int = 3) -> None:
        self.tier = tier

    def __sklearn_is_fitted__(self) -> bool:
        return True

    def fit(self, x=None, y=None, **kwargs):  # noqa: ARG002
        # check_is_fitted 会先要求对象"有 fit"（否则报 not an estimator instance）
        return self

    def predict_proba(self, x):
        row = [0.1, 0.1, 0.1, 0.1]
        row[self.tier] = 0.7
        return [row for _ in range(len(x))]

    def predict(self, x):
        return [self.tier for _ in range(len(x))]


def _write_artifact(path: Path, *, sem_dim: int = 0, tier: int = 3) -> None:
    """写一个最小产物；真模型太慢，这里只验分支选择与评估链路。"""
    import joblib

    payload = {
        "format": 1, "vectorizer": None, "model": _StubModel(tier),
        "feature_keys": list(FEATURE_KEYS), "trained_at": 0.0,
        "n_samples": 1, "val_accuracy": None, "sem_dim": sem_dim,
        "head": None, "head_dim": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, path)


def test_fallback_chain_prefers_semantic_then_nosem(adaptive_dir):
    """语义版需要编码器；编码器不可用时落到无语义兜底产物（方案 10 §4.5）。"""
    from routivus.adaptive.store import ml_router_nosem_path

    _write_artifact(ml_router_path(), sem_dim=512)   # 语义版：要编码器
    _write_artifact(ml_router_nosem_path(), sem_dim=0)  # 无语义兜底

    ml = MLRouter(semantic=_NoEncoder())
    assert ml.available, f"source={ml.source!r} reason={ml.unavailable_reason!r}"
    assert ml.source == "nosem"

    ml2 = MLRouter(semantic=_StubEncoder())
    assert ml2.available, f"source={ml2.source!r} reason={ml2.unavailable_reason!r}"
    assert ml2.source == "semantic"


def test_semantic_artifact_without_any_fallback_reports_reason(adaptive_dir):
    """显式路径 + 编码器不可用 → 不可用 + 原因码 no_semantic。

    走 ``artifact_path`` 是刻意的：默认构造会先 ``ensure_default_artifacts()``
    把随包的无语义兜底落位，那条路是可用的（见下一个用例）。
    """
    _write_artifact(ml_router_path(), sem_dim=512)
    ml = MLRouter(artifact_path=ml_router_path(), semantic=_NoEncoder())
    assert not ml.available
    assert ml.unavailable_reason == "no_semantic", ml.unavailable_reason


def test_bundled_nosem_artifact_rescues_broken_onnxruntime(adaptive_dir):
    """空数据目录 + 编码器不可用 → 随包无语义兜底被落位并接住（方案 10 §4.5）。

    这正是"环境里 onnxruntime 装坏"那台机器的路径：整层 ML 精判不该消失。
    """
    ml = MLRouter(semantic=_NoEncoder())
    assert ml.available and ml.source == "nosem"
    assert (adaptive_dir / "router.lgb.nosem").exists()


def test_unavailable_reason_no_artifact(adaptive_dir):
    ml = MLRouter(artifact_path=adaptive_dir / "absent.lgb")
    assert not ml.available
    assert ml.unavailable_reason == "no_artifact"


def test_semantic_encoder_reports_missing_artifact(tmp_path):
    encoder = SemanticEncoder(tmp_path / "absent.onnx")
    assert not encoder.available
    assert encoder.unavailable_reason == "artifact_missing"


# ---------------------------------------------------------------------------
# 本地样本库（批次 2）
# ---------------------------------------------------------------------------

def test_sem_samples_roundtrip_and_privacy(adaptive_dir):
    vec = [0.1, -0.2, 0.3] + [0.0] * 509
    assert samples_mod.write_sem_sample("hash-1", vec)
    back = samples_mod.read_sem_samples()
    assert "hash-1" in back
    assert len(back["hash-1"]) == 512
    assert back["hash-1"][0] == pytest.approx(0.1, abs=0.02)

    # 隐私硬要求：文件里不许出现原文（只有 hash + 量化向量）
    raw = sem_samples_path().read_text(encoding="utf-8")
    record = json.loads(raw.splitlines()[0])
    assert set(record) == {"ts", "text_hash", "sem"}
    assert "text" not in record


def test_sem_sample_skipped_without_vector(adaptive_dir):
    assert not samples_mod.write_sem_sample("hash-2", None)
    assert not samples_mod.write_sem_sample("", [0.1])
    assert not sem_samples_path().exists()


# ---------------------------------------------------------------------------
# 门槛与不劣门（批次 2/3）
# ---------------------------------------------------------------------------

def _feedback_record(text_hash: str, tier: str, signal: str, ts: float) -> dict:
    return {
        "ts": ts, "text_hash": text_hash, "model_tier": tier, "signal": signal,
        "weight": 1.0, "features": {"len_chars": 30, "num_impl_kw": 1},
    }


def _write_feedback(records: list[dict]) -> None:
    from routivus.adaptive.store import feedback_log_path

    path = feedback_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")


def _enough_records(per_tier: int = 20, tiers=("Basic", "Enhanced")) -> list[dict]:
    """每档 ``per_tier`` 条 upgrade 信号（标签 = 档位 +1）。"""
    out: list[dict] = []
    ts = 1.0
    for tier in tiers:
        for i in range(per_tier):
            out.append(_feedback_record(f"{tier}-{i}", tier, "upgrade", ts))
            ts += 1.0
    return out


def _samples_from(records: list[dict]) -> list[dict]:
    """feedback 记录 → 训练样本（gate 吃的是样本，不是原始记录）。"""
    from routivus.adaptive.training import build_samples

    return build_samples(None, records)[0]


def test_gate_reasons(adaptive_dir):
    state = evolve.EvolveState()
    # 只有一个档位
    one_tier = _samples_from(
        [_feedback_record(f"b{i}", "Basic", "upgrade", i) for i in range(50)],
    )
    assert evolve.gate(one_tier, state) == "not_enough_tiers"
    # 档位够但每档不足
    assert evolve.gate(_samples_from(_enough_records(per_tier=5)), state) == \
        "not_enough_per_tier"
    # 每档够但总量不到首次门槛（40 < MIN_TOTAL_FIRST=120）
    assert evolve.gate(_samples_from(_enough_records(per_tier=20)), state) == \
        "not_enough_total"
    # 非首次 → MIN_TOTAL=60 满足；默认 last_attempt_at=0 不触发冷却
    state.evolve_count = 1
    big = _samples_from(_enough_records(per_tier=30))
    assert evolve.gate(big, state) == ""
    # 冷却期内
    state.last_attempt_at = 10_000.0
    assert evolve.gate(big, state, now=10_000.0 + 3600.0) == "cooldown"


def test_skip_records_log_but_keeps_state(adaptive_dir):
    _write_feedback(_enough_records(per_tier=3))  # 样本远远不够
    result = evolve.run_evolve("manual")
    assert result["decision"] == "skipped"
    assert result["reason"] in {"not_enough_per_tier", "not_enough_total", "not_enough_tiers"}
    # 门槛不过不该烧掉冷却期
    assert evolve.read_state().last_attempt_at == 0.0
    assert evolve_log_path().exists()
    assert not ml_router_path().exists()


@pytest.mark.skipif(not _ml_available(), reason="未安装 lightgbm")
class TestReplaceGuard:
    def test_rejected_when_worse(self, adaptive_dir, monkeypatch):
        """不劣门：候选更差 → 拒绝替换，产物保持原样。"""
        _write_feedback(_enough_records(per_tier=30))
        _write_artifact(ml_router_path())
        before = ml_router_path().read_bytes()

        from routivus.adaptive import training

        accs = iter([0.40, 0.80])  # 先评估候选，再评估基线
        monkeypatch.setattr(training, "artifact_accuracy", lambda *a, **k: next(accs))

        result = evolve.run_evolve("manual", force=True)
        assert result["decision"] == "skipped"
        assert result["reason"] == "worse_than_current"
        assert ml_router_path().read_bytes() == before  # 没被替换
        assert not ml_router_path().with_name(ml_router_path().name + ".evolving").exists()
        state = evolve.read_state()
        assert state.last_decision == "rejected" and state.evolve_count == 0

    def test_replaced_when_better(self, adaptive_dir, monkeypatch):
        """接受：备份 .prev、原子落位、状态与日志更新。"""
        _write_feedback(_enough_records(per_tier=30))
        _write_artifact(ml_router_path())
        before = ml_router_path().read_bytes()

        from routivus.adaptive import training

        accs = iter([0.90, 0.50])
        monkeypatch.setattr(training, "artifact_accuracy", lambda *a, **k: next(accs))

        result = evolve.run_evolve("manual", force=True)
        assert result["decision"] == "replaced"
        assert ml_router_path().exists()
        assert ml_router_path().read_bytes() != before
        assert ml_router_prev_path().read_bytes() == before  # 可回滚
        state = evolve.read_state()
        assert state.evolve_count == 1 and state.last_decision == "replaced"
        assert state.samples_since_success == 0
        assert state.last_holdout_acc == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# reset --hard
# ---------------------------------------------------------------------------

def test_reset_hard_clears_local_artifacts(adaptive_dir):
    _write_artifact(ml_router_path())
    calibration_path().write_text("{}", encoding="utf-8")
    learned_rules_path().write_text("{}", encoding="utf-8")
    sem_samples_path().write_text("{}\n", encoding="utf-8")
    ml_router_prev_path().write_bytes(b"prev")
    evolve_state_path().write_text("{}", encoding="utf-8")

    removed = reset_adaptive_data(hard=True)
    assert {"calibration.json", "learned_rules.json", "router.lgb", "router.lgb.prev",
            "sem_samples.jsonl", "evolve_state.json"} <= set(removed)
    assert not ml_router_path().exists()
    assert not ml_router_prev_path().exists()
    assert not sem_samples_path().exists()


def test_reset_soft_keeps_artifacts(adaptive_dir):
    _write_artifact(ml_router_path())
    reset_adaptive_data()  # 默认软清
    assert ml_router_path().exists()


# ---------------------------------------------------------------------------
# L3 语义头（批次 4）
# ---------------------------------------------------------------------------

def _sem_samples(per_tier: int, tiers=(0, 1)) -> list[dict]:
    out: list[dict] = []
    for tier in tiers:
        for i in range(per_tier):
            vec = [0.0] * 512
            vec[tier] = 1.0          # 两档正交，质心可分
            vec[100 + (i % 3)] = 0.1
            out.append({"tier": tier, "weight": 1.0, "sem": vec,
                        "features": {}, "text": "", "text_hash": f"{tier}-{i}"})
    return out


def test_semantic_head_needs_enough_samples():
    assert semantic_head.train_head([]) is None
    assert semantic_head.train_head(_sem_samples(3)) is None  # 每档 < 8


def test_semantic_head_trains_and_predicts():
    head = semantic_head.train_head(_sem_samples(20))
    assert head is not None
    assert len(head.centroids) == 2
    assert head.holdout_acc is not None and head.holdout_acc > 0.8
    probs = head.probs(_sem_samples(1)[0]["sem"])
    assert len(probs) == 4 or len(probs) == 2
    assert sum(probs) == pytest.approx(1.0, abs=1e-6)
    feats = head.features_for(_sem_samples(4), [0, 1, 2, 3])
    assert len(feats) == 4 and all(len(row) == len(head.centroids) for row in feats)


@pytest.mark.skipif(not _ml_available(), reason="未安装 lightgbm")
def test_artifact_accuracy_uses_embedded_head(tmp_path):
    """带语义头的产物能被评估（列宽自洽，不必补零）。"""
    from routivus.adaptive.training import train_and_save

    head = semantic_head.train_head(_sem_samples(20))
    samples = _sem_samples(20)
    out = tmp_path / "with_head.lgb"
    train_and_save(samples, out, head=head)
    payload = load_artifact(out)
    assert payload is not None
    # 头列宽 = 参与训练的档位数（这里刻意只给 2 档，列宽就是 2，不是写死的 4）
    assert payload["head_dim"] == len(head.centroids) == 2
    assert payload["head"] is not None
    acc = artifact_accuracy(payload, samples)
    assert acc is not None and 0.0 <= acc <= 1.0
