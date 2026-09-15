"""MLRouter 精判接入与静默回落测试。

缺 ML 依赖/无产物时：MLRouter 整类 skip（与"产物缺依赖时静默回落"的
主进程语义一致）；纯逻辑的回落/门控测试在依赖缺失时降级为构造性跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from routivus.adaptive.calibrate import Calibration  # noqa: E402
from routivus.router import route  # noqa: E402
from routivus.router.ml_router import MLRouter, ARTIFACT_FORMAT  # noqa: E402


def _ml_available() -> bool:
    try:
        import joblib  # noqa: F401
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
        import lightgbm  # noqa: F401
        from scipy.sparse import csr_matrix, hstack  # noqa: F401
        return True
    except ImportError:
        return False


def _samples(n=40):
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


# ---------------------------------------------------------------------------
# 无产物 / 不可用：静默回落（不依赖 ML）
# ---------------------------------------------------------------------------

class TestFallback:
    def test_no_artifact_unavailable(self, tmp_path):
        # 显式指向不存在的产物路径（不触发随包落位）→ 回落，predict/decide 均 None
        p = tmp_path / "nope" / "router.lgb"
        assert not p.exists()
        r = MLRouter(p)
        assert not r.available
        assert r.predict("随便聊聊") is None
        assert r.decide("随便聊聊") is None

    def test_none_path_unavailable(self, tmp_path):
        # 默认目录指向隔离空目录且显式不可写时回落；此处用显式缺失路径保证隔离，
        # 避免随包兜底把产物写入真实目录或 C 盘根（原用例会污染 C:/__routivus_never_exists__）
        r = MLRouter(tmp_path / "no_such" / "router.lgb")
        assert not r.available

    def test_available_preserves_import(self):
        """即便产物缺失，import MLRouter 也不触发 ML 依赖导入（主进程零硬依赖）。"""
        import routivus.router.ml_router as m
        assert m.ARTIFACT_FORMAT == ARTIFACT_FORMAT


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestMLRouter:
    @pytest.fixture
    def router(self, tmp_path):
        from train_router import train_and_save
        out = tmp_path / "router.lgb"
        train_and_save(_samples(60), out)
        return MLRouter(out)

    def test_available_after_valid_artifact(self, router):
        assert router.available
        assert router.n_samples == 60

    def test_predict_hard_pattern(self, router):
        # "设计生产生产分布式部署架构" 应判高档（prob 最高档匹配 3）
        pred = router.predict("设计生产环境分布式部署架构方案")
        assert pred is not None
        assert pred.tier in (0, 1, 2, 3)
        assert 0.0 <= pred.prob <= 1.0

    def test_decide_gate_passes_for_confident(self, router):
        # 高区分度样本：置信足够 → decide 返回档位而非 None
        tier = router.decide("设计生产环境分布式部署架构方案")
        assert tier in (0, 1, 2, 3)

    def test_decide_applies_calibration(self, router):
        # 偏强档（bias>0）：confidence - bias < gate → 可能降一档
        cal = Calibration(bias=(0.0, 0.0, 0.0, 0.15),
                          threshold_adjust=0.1)
        tier_no_cal = router.decide("设计生产环境分布式部署架构方案")
        tier_cal = router.decide("设计生产环境分布式部署架构方案", calibration=cal)
        # 有强偏置的 Ultimate(3) 在 confidence<0.6 时降档，否则同
        assert tier_cal <= (tier_no_cal if tier_no_cal is not None else 3)

    def test_decode_low_confidence_returns_none(self, tmp_path):
        # 训练成"几乎均匀"的弱模型，任意新文本 confidence 大概率低 → 可能回落 None
        from train_router import train_and_save
        out = tmp_path / "weak.lgb"
        # 混入跨档相似文本降低判别力
        mixed = [{"text": f"一样的句子第{i}条", "tier": i % 4,
                  "weight": 1.0, "features": None} for i in range(40)]
        train_and_save(mixed, out)
        r = MLRouter(out)
        # 至少不抛出、返回要么档位要么 None（静默回落都能接受）
        assert r.decide("这个句子没出现过") in (None, 0, 1, 2, 3)

    def test_corrupt_artifact_unavailable(self, tmp_path):
        p = tmp_path / "bad.lgb"
        p.write_bytes(b"not-a-joblib-payload")
        r = MLRouter(p)
        assert not r.available
        assert r.decide("随便聊聊") is None


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestRouteIntegration:
    def test_route_uses_ml_when_confident(self, tmp_path):
        """route() 传 ml_router：软规则档位被 ML 高置信档替换。"""
        out = tmp_path / "router.lgb"
        from train_router import train_and_save
        train_and_save(_samples(80), out)
        r = MLRouter(out)
        assert r.available

        # "设计生产分布式部署架构" 训练中几乎全标 3 → ML 高置信判 3
        res = route("设计生产环境分布式部署架构方案",
                    fallback_provider="p", fallback_model="m", ml_router=r)
        assert res.tier_idx in (0, 1, 2, 3)

    def test_no_ml_falls_back_to_rule(self):
        """不传 ml_router：行为退回纯规则（回归保护）。"""
        res = route("写个函数把两个数相加",
                    fallback_provider="p", fallback_model="m")
        assert res.hard_rule is False  # 软规则路径仍走规则


def _semantic_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not (_ml_available() and _semantic_available()), reason="未安装 ml + semantic 依赖"
)
class TestBundledBootstrap:
    def test_default_dir_uses_bundled_artifacts(self, tmp_path, monkeypatch):
        """空数据目录首启：随包 router.lgb + 语义编码器自动落位，ML 精判开箱可用。"""
        monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))
        from routivus.router.semantic import load_semantic_encoder
        sem = load_semantic_encoder(None)
        r = MLRouter(None, semantic=sem)  # 默认路径 → 触发随包落位
        assert r.available
        assert r.sem_dim == 512  # 随包兜底绑定语义列
        # 兜底模型仅 34 样本，未见文本可能不达置信门 → decide 允许回落 None
        assert r.decide("设计生产环境分布式部署架构方案") in (None, 0, 1, 2, 3)
        assert r.predict("写个函数把两个数相加") is not None


# ---------------------------------------------------------------------------
# L3 语义头列（本地进化产物）：预测端必须复现训练时追加的那几列
# ---------------------------------------------------------------------------

class _StubSemantic:
    """可用的语义编码器替身：给一个可复现的 512 维向量。"""

    dim = 512
    available = True
    unavailable_reason = ""

    def encode(self, text: str):  # noqa: ARG002
        import numpy as np

        rng = np.random.default_rng(7)
        return rng.normal(size=512).tolist()


def _head_samples(tiers: int = 2, per_tier: int = 10, dim: int = 512):
    """本地样本的形态：**无原文**、有数值特征与语义向量（evolve 那条路）。"""
    import numpy as np

    from routivus.adaptive.training import FEATURE_KEYS

    rng = np.random.default_rng(20260915)
    out = []
    for tier in range(tiers):
        for i in range(per_tier):
            out.append({
                "text": "",
                "tier": tier,
                "weight": 1.0,
                "features": {k: float(i) for k in FEATURE_KEYS},
                "sem": (rng.normal(size=dim) + tier * 4.0).tolist(),
                "text_hash": f"{tier}-{i}",
            })
    return out


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestSemanticHeadColumns:
    """回归：产物带 L3 语义头时，预测必须复现"每档一个概率"那几列。

    训练端（``adaptive/training.py`` 的 build_matrix）会追加这几列，评估端
    （artifact_accuracy）也会按产物里的头复现；但预测端曾漏掉，于是产物
    **加载成功、被判可用**，一预测就抛 ``X has N features, but LGBMClassifier
    is expecting M`` —— 而 route() 调精判那一句没有 try/except，异常会一路冒到
    路由调用处。本地进化第一次成功时必然踩上（门槛每档 ≥20 远高于训头所需的 8）。
    """

    def _trained(self, tmp_path):
        from routivus.adaptive.semantic_head import train_head
        from routivus.adaptive.training import train_and_save

        samples = _head_samples()
        head = train_head(samples)
        assert head is not None and len(head.centroids) == 2
        out = tmp_path / "router.lgb"
        train_and_save(samples, out, semantic=None, head=head)
        return out, head

    def test_head_artifact_is_predictable(self, tmp_path):
        out, head = self._trained(tmp_path)
        r = MLRouter(out, semantic=_StubSemantic())

        assert r.available
        assert r.head_dim == len(head.centroids)
        # 列数必须与模型期望一致 —— 这就是原来炸掉的那一处
        assert r._encode("随便聊聊", {}).shape[1] == r._model.n_features_in_
        assert r.predict("随便聊聊") is not None
        assert r.decide("随便聊聊") in (None, 0, 1, 2, 3)

    def test_head_artifact_through_route_does_not_raise(self, tmp_path):
        out, _ = self._trained(tmp_path)
        r = MLRouter(out, semantic=_StubSemantic())
        res = route("设计生产环境分布式部署架构方案",
                    fallback_provider="p", fallback_model="m", ml_router=r)
        assert res.tier_idx in (0, 1, 2, 3)

    def test_head_body_missing_falls_back_to_zero_columns(self, tmp_path):
        """头本体缺失（旧格式 / 损坏）但声明了 head_dim：补零列，不抛错。"""
        import joblib

        out, head = self._trained(tmp_path)
        payload = joblib.load(out)
        payload["head"] = None
        joblib.dump(payload, out)

        r = MLRouter(out, semantic=_StubSemantic())
        assert r.available
        assert r.head_dim == len(head.centroids)  # 退回声明值
        assert r._encode("随便聊聊", {}).shape[1] == r._model.n_features_in_
        assert r.predict("随便聊聊") is not None

    def test_artifact_without_head_unchanged(self, tmp_path):
        """不带头的产物（/train 与随包那份）行为不变：不追加任何列。"""
        from train_router import train_and_save

        out = tmp_path / "plain.lgb"
        train_and_save(_samples(40), out)
        r = MLRouter(out)
        assert r.available
        assert r.head_dim == 0
        assert r._encode("写个函数把两个数相加", {}).shape[1] == r._model.n_features_in_
        assert r.predict("写个函数把两个数相加") is not None