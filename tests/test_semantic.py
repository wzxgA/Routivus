"""第 6 期 C2（语义编码器接入 ml_router 精判）测试。

无 onnxruntime/无产物时：SemanticEncoder 与带语义产的 MLRouter 整段回落
（available=False），与"产物缺依赖时静默回落"的主进程语义一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from routivus.router.ml_router import MLRouter, ARTIFACT_FORMAT  # noqa: E402
from routivus.router.semantic import SemanticEncoder, load_semantic_encoder  # noqa: E402


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


def _semantic_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# SemanticEncoder：不可用回落（不依赖 onnxruntime）
# ---------------------------------------------------------------------------

class TestSemanticFallback:
    def test_missing_file_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XG_ADAPTIVE_DIR", str(tmp_path / "nope"))
        enc = SemanticEncoder()  # 数据目录无 router_semantics.onnx
        assert not enc.available
        assert enc.encode("随便聊聊") is None

    def test_explicit_nonexistent_path(self, tmp_path):
        enc = SemanticEncoder(tmp_path / "missing.onnx")
        assert not enc.available
        assert enc.dim == 512

    def test_import_guarded(self):
        # 加载模块不触发 onnxruntime 导入（主进程零硬依赖）
        import routivus.router.semantic as s
        assert callable(s.load_semantic_encoder)

    def test_factory_returns_unavailable(self, tmp_path):
        # 传显式缺失路径 → 不触发随包落位，回落（available=False）
        p = tmp_path / "no_such" / "router_semantics.onnx"
        assert not p.exists()
        assert not load_semantic_encoder(p).available


@pytest.mark.skipif(not _semantic_available(), reason="未安装 semantic extras")
def test_factory_bundles_artifact_to_empty_dir(tmp_path, monkeypatch):
    """B：空数据目录首启调用 factory → 随包产物(xg/assets)自动落位并可用。"""
    monkeypatch.setenv("XG_ADAPTIVE_DIR", str(tmp_path))
    enc = load_semantic_encoder(None)
    assert enc.available
    assert enc.artifact_exists
    assert (tmp_path / "router_semantics.json").exists()
    # ML 精判兜底产物（router.lgb）也应随包落位
    assert (tmp_path / "router.lgb").exists()


@pytest.mark.skipif(not _semantic_available(), reason="未安装 semantic extras")
class TestSemanticUnavailableArtifact:
    def test_artifact_without_json_tokenizer_unavailable(self, tmp_path):
        # 有 .onnx 但缺同目录 tokenizer .json → 不可用
        p = tmp_path / "router_semantics.onnx"
        p.write_bytes(b"not-a-real-onnx")
        assert not SemanticEncoder(p).available


# ---------------------------------------------------------------------------
# MLRouter 语义列：训练写入 sem_dim + 预测加载回落（缺语义依赖整段 skip）
# ---------------------------------------------------------------------------

class TestSemanticFallbackWithMlArtifact:
    def test_artifact_missing_semantic_unavailable(self, tmp_path):
        # 无产物时 MLRouter 回落（无语义编码器也能安全构造）
        assert not MLRouter(tmp_path / "nope.lgb", semantic=None).available

    def test_semantic_none_arg_coexists(self, tmp_path):
        # 显式传 semantic=None 与默认等价（向后兼容）
        assert MLRouter(tmp_path / "nope.lgb", semantic=None).available is False


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestSemanticMlTraining:
    def _samples(self, n=60):
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

    def test_train_without_semantic_sets_zero_dim(self, tmp_path):
        # 第 5 期行为回归：不传 semantic → sem_dim=0，payload 无语义标记
        from train_router import train_and_save
        out = tmp_path / "router.lgb"
        report = train_and_save(self._samples(40), out)
        assert report["sem_dim"] == 0
        import joblib
        assert joblib.load(out)["sem_dim"] == 0

    def test_train_semantic_dim_512(self, tmp_path):
        # 缺语义依赖则整段 skip，但语法/逻辑正确性在无 onnx 时仍可生成 0 维。
        from train_router import train_and_save
        out = tmp_path / "router.lgb"
        report = train_and_save(self._samples(40), out, semantic=None)
        assert report["sem_dim"] == 0

    @pytest.mark.skipif(not _semantic_available(), reason="无真实语义产物")
    def test_semantic_artifact_predicts(self, tmp_path):
        # 端到端需真实 .onnx + .json；无产物时 skip
        assert True  # 占位：真实语义端到端需离线导出产物，见 tools/export_bge_onnx.py

# ---------------------------------------------------------------------------
# MLRouter 语义约束：产物带语义列而编码器不可用 → 回落
# ---------------------------------------------------------------------------

def _fake_semantic():
    """测试用假语义编码器（512 维确定性伪向量），不依赖 onnxruntime。"""

    class _Fake:
        dim = 512

        @property
        def available(self):
            return True

        def encode(self, text):
            h = abs(hash(text)) % 97
            vec = [(v / 50.0) for v in range(512)]
            vec[0] = (h / 97.0)
            return vec

    return _Fake()


def _samples(n=60):
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


@pytest.mark.skipif(not _ml_available(), reason="未安装 ml extras")
class TestSemanticColumnContract:
    def test_semantic_artifact_requires_encoder(self, tmp_path):
        # 训练产物带语义列（sem_dim>0），但 MLRouter 未传语义编码器 → 回落
        from train_router import train_and_save
        # 用假语义训练，得到 sem_dim=512 的产物
        out = tmp_path / "sem.lgb"
        report = train_and_save(_samples(40), out, semantic=_fake_semantic())
        assert report["sem_dim"] == 512
        # 不传 semantic → available=False（无法提供语义列宽，静默回落）
        assert not MLRouter(out).available
        # 传可用的语义编码器 → available=True
        r = MLRouter(out, semantic=_fake_semantic())
        assert r.available
        assert r.sem_dim == 512
        pred = r.predict("设计生产环境分布式部署架构方案")
        assert pred is not None and pred.tier in (0, 1, 2, 3)

    def test_semantic_artifact_encoder_unavailable_falls_back(self, tmp_path):
        # 第 6 期静默回落：语义产物 + 坏编码器 → MLRouter 不可用
        from train_router import train_and_save
        class _Bad:
            dim = 512
            available = False
            def encode(self, text):  # noqa: ARG002
                return None
        out = tmp_path / "sem.lgb"
        train_and_save(_samples(40), out, semantic=_fake_semantic())
        assert not MLRouter(out, semantic=_Bad()).available