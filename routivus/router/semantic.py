"""SmartRouter 语义编码器（第 6 期 C2，ONNX Runtime 加载 bge 产物）。

职责：把用户输入编码为 512 维语义向量，供 ml_router 精判时并入特征列
（``[TF-IDF + 数值 + 语义]``）；任何缺依赖 / 缺产物 / 产物损坏 情形一律
**静默回落**（encode 返回 None），行为退化为 ``ml_router`` 无语义时的原貌。

导入保护（主进程零硬性 ML 依赖）：
    onnxruntime / tokenizers / numpy 仅在 encode 运行期 import，任一把
    import 失败即在初始化时把本编码器标记为不可用（available=False），
    主进程绝不因缺依赖而报错。
"""

from __future__ import annotations

from pathlib import Path

_EMBED_DIM = 512  # bge-small-zh-v1.5 输出维度


def _default_onnx_path() -> Path:
    from ..adaptive.store import semantic_onnx_path
    return semantic_onnx_path()


class SemanticEncoder:
    """ONNX 语义编码器；不可用时 encode 返回 None（静默回落）。"""

    def __init__(self, onnx_path: Path | None = None) -> None:
        self._path = onnx_path or _default_onnx_path()
        self._session = None
        self._tokenizer = None
        # C3 观测：成功编码统计（每轮耗时 + 有效样本量），供 /smartRouter status 展示
        self._calls = 0
        self._total_ms = 0.0
        self._last_ms = 0.0
        self._load()

    def _load(self) -> None:
        """加载 ONNX session + tokenizer；任何异常 → 不可用。"""
        if not self._path.exists():
            return
        try:
            import onnxruntime  # noqa: PLC0415
            from tokenizers import Tokenizer  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415

            # bge 的 tokenizer 以 JSON 与产物同级目录存放（C1 导出时同目录）
            tok_path = self._path.with_suffix(".json")
            if not tok_path.exists():
                return
            tok = Tokenizer.from_file(str(tok_path))
            sess = onnxruntime.InferenceSession(
                str(self._path), providers=["CPUExecutionProvider"],
            )
            # 自检输出维度 = 512
            out_meta = sess.get_outputs()[0].shape
            dims = [d for d in out_meta if d not in (None, -1)]
            if dims and dims[-1] != _EMBED_DIM:
                return
            self._session = sess
            self._tokenizer = tok
        except Exception:
            self._session = None
            self._tokenizer = None

    @property
    def available(self) -> bool:
        return self._session is not None and self._tokenizer is not None

    @property
    def dim(self) -> int:
        return _EMBED_DIM

    @property
    def artifact_exists(self) -> bool:
        """产物（.onnx）是否存在于预期路径（C3 观测）。"""
        return self._path.exists()

    @property
    def calls(self) -> int:
        """成功完成编码的轮次数（有效样本量观测）。"""
        return self._calls

    @property
    def avg_ms(self) -> float:
        """累计平均每轮编码耗时（毫秒）。"""
        return (self._total_ms / self._calls) if self._calls else 0.0

    @property
    def last_ms(self) -> float:
        """最近一轮编码耗时（毫秒）。"""
        return self._last_ms

    def encode(self, text: str) -> list[float] | None:
        """把整段文本编码为 512 维向量；不可用返回 None（静默回落）。

        bge 取 first token（[CLS]）向量并做 L2 归一化；成功时记录轮次耗时。
        """
        if not self.available or not text:
            return None
        import time  # noqa: PLC0415
        t0 = time.perf_counter()
        try:
            import numpy as np  # noqa: PLC0415
            enc = self._tokenizer.encode(text)
            ids = (enc.ids[:512] or [0]).__class__([  # 空输入兜底
                i for i in enc.ids[:512]
            ] or [0])[:512]
            mask = [1] * len(ids)
            input_ids = np.array([ids], dtype=np.int64)
            attention = np.array([mask], dtype=np.int64)
            feed = {"input_ids": input_ids, "attention_mask": attention}
            # bge/BERT 常要求 token_type_ids；单段输入给全零
            if "token_type_ids" in {i.name for i in self._session.get_inputs()}:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            out = self._session.run(None, feed)[0]  # (1, seq, dim)
            vec = out[0][0].astype(np.float64)  # [CLS]
            norm = float(np.linalg.norm(vec))
            if norm < 1e-9:
                return [0.0] * _EMBED_DIM
            self._last_ms = (time.perf_counter() - t0) * 1000.0
            self._total_ms += self._last_ms
            self._calls += 1
            return (vec / norm).tolist()
        except Exception:
            return None


def load_semantic_encoder(onnx_path: Path | None = None) -> SemanticEncoder:
    """便捷工厂：缺依赖/缺产物时返回一个 available=False 的编码器。

    未显式指定路径时，先尝试把随包语义产物（xg/assets/）落位到数据目录，
    使 clone 后首启即可用语义通道；无随包产物或复制失败时静默跳过。
    """
    if onnx_path is None:
        from ..adaptive.store import ensure_default_artifacts
        ensure_default_artifacts()
    return SemanticEncoder(onnx_path)