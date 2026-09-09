"""bge → ONNX 导出脚本测试。

纯校验逻辑（embed 维度 / 余弦一致性 / 延迟）无 ML 依赖直接测；
真实导出端到端缺 semantic extras（torch/optimum/onnxruntime）时整段 skip，
与"产物缺依赖静默回落"语义一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from export_bge_onnx import (  # noqa: E402
    DEFAULT_MODEL, MIN_COSINE, check_cosine, check_embed_dim, check_latency,
    cosine_similarity, default_onnx_path,
)


# ---------------------------------------------------------------------------
# 纯校验逻辑（无依赖直测）
# ---------------------------------------------------------------------------

class TestEmbedDim:
    def test_ok_512(self):
        check_embed_dim((1, 512))   # batch=1
        check_embed_dim((1, 1, 512))  # (batch, seq, dim)

    def test_wrong_dim_raises(self):
        with pytest.raises(ValueError):
            check_embed_dim((1, 768))   # bge-base 维度，非 small


class TestCosine:
    def test_identical_is_one(self):
        v = [0.5, 0.5, 0.5, 0.5]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_is_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            cosine_similarity([1.0], [1.0, 2.0])

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_check_below_threshold_raises(self):
        check_cosine([1.0, 0.0], [0.999, 0.0])  # 接近 1，通过
        with pytest.raises(ValueError):
            check_cosine([1.0, 0.0], [0.5, 0.5], min_cosine=0.9)


class TestLatency:
    def test_under_threshold_ok(self):
        check_latency(80.0)          # 80ms < 100ms，通过

    def test_over_threshold_raises(self):
        with pytest.raises(ValueError):
            check_latency(120.0)     # 120ms > 100ms

    def test_custom_threshold(self):
        check_latency(50.0, max_ms=100.0)
        with pytest.raises(ValueError):
            check_latency(150.0, max_ms=100.0)


class TestDefaults:
    def test_default_model(self):
        assert DEFAULT_MODEL == "BAAI/bge-small-zh-v1.5"

    def test_default_path_under_adaptive(self):
        # 默认产物路径应位于数据目录，文件名 router_semantics.onnx
        from routivus.adaptive.store import SEMANTIC_ONNX
        p = default_onnx_path()
        assert p.name == SEMANTIC_ONNX

    def test_min_cosine_threshold(self):
        assert 0.0 < MIN_COSINE < 1.0


# ---------------------------------------------------------------------------
# 真实导出（缺 semantic extras 整类 skip）
# ---------------------------------------------------------------------------

def _semantic_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        from optimum.onnxruntime import ORTModelForFeatureExtraction  # noqa: F401
        from transformers import AutoTokenizer  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _semantic_available(), reason="未安装 semantic extras")
class TestExportEndToEnd:
    def test_export_produces_artifact(self, tmp_path):
        from export_bge_onnx import export_and_quantize
        out = tmp_path / "router_semantics.onnx"
        report = export_and_quantize(DEFAULT_MODEL, out)
        assert out.exists() and out.stat().st_size > 0
        assert report["dim"] == 512
        assert report["latency_ms"] <= 100.0