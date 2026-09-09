"""SmartRouter ML 精判接入与静默回落（第 5 期 B2）。

职责：加载离线训练的产物（tools/train_router.py 生成的 router.lgb），
在规则路由之后、安全后处理之前参与精判——取"概率最高档"，受 置信门 +
校准偏置 约束；任何缺依赖 / 缺产物 / 产物损坏 情形一律**静默回落**，
行为退化为纯规则（与本模块不存在的第 4 期行为完全一致）。

导入保护（主进程零硬性 ML 依赖）：
    joblib / lightgbm / sklearn / numpy 仅在 onnx 运行期 import，
    任一把 import 失败即在初始化时把本模块标记为不可用（available=False），
    主进程绝不因缺依赖而报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..adaptive.calibrate import CONFIDENCE_BASE, apply_calibration

# 产物格式版本（与 tools/train_router.py 的 ARTIFACT_FORMAT 一致）
ARTIFACT_FORMAT = 1
_DEFAULT_ARTIFACT = "router.lgb"  # 默认文件名，相对 data_dir


@dataclass(frozen=True)
class MLPrediction:
    """一次可用的 ML 精判结果。"""

    tier: int        # 概率最高档 0..3
    prob: float      # 该档预测概率 0..1


class MLRouter:
    """产物加载与精判；不可用时全链路静默回落。"""

    def __init__(self, artifact_path: Path | None = None,
                 semantic=None) -> None:
        self._payload = None
        self._vectorizer = None
        self._model = None
        self._feature_keys: tuple[str, ...] = ()
        # 第 6 期 C2：可选语义编码器（bge 512 维）。训练产物带 sem_dim>0 时，
        # 预测需语义列；语义不可用则整个静默回落（无法提供既定列宽）。
        self._semantic = semantic
        self._sem_dim = 0
        # 随包兜底：默认产物缺失时首启自动落位（clone 后开箱可用）
        if artifact_path is None:
            self._ensure_bundled()
        self._load(artifact_path)

    def _ensure_bundled(self) -> None:
        """把随包默认产物（router.lgb 等）复制到数据目录；已存在则跳过。"""
        from ..adaptive.store import ensure_default_artifacts
        ensure_default_artifacts()

    # -- 加载 -----------------------------------------------------------
    def _load(self, artifact_path: Path | None) -> None:
        """加载产物；任何异常（缺依赖/缺文件/损坏/格式不符）→ 不可用。"""
        path = artifact_path or self._default_path()
        if path is None or not path.exists():
            return
        try:
            import joblib  # noqa: PLC0415
            from sklearn.utils.validation import check_is_fitted  # noqa: PLC0415
            payload = joblib.load(path)
            if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
                return
            model = payload.get("model")
            vec = payload.get("vectorizer")
            keys = payload.get("feature_keys") or ()
            if not hasattr(model, "predict_proba"):
                return
            check_is_fitted(model)
            sem_dim = payload.get("sem_dim", 0) or 0
            # 产物声明需要语义列，但语义编码器不可用 → 整链静默回落
            if sem_dim > 0 and (self._semantic is None or not self._semantic.available):
                return
            self._vectorizer = vec
            self._model = model
            self._feature_keys = tuple(keys)
            self._sem_dim = int(sem_dim)
            self._payload = payload
        except Exception:
            self._payload = None
            self._vectorizer = None
            self._model = None
            self._feature_keys = ()
            self._sem_dim = 0

    def _default_path(self) -> Path | None:
        from ..adaptive.store import data_dir
        return data_dir() / _DEFAULT_ARTIFACT

    @property
    def available(self) -> bool:
        """产物已成功加载且模型可用。"""
        return self._model is not None

    @property
    def n_samples(self) -> int | None:
        return self._payload.get("n_samples") if self._payload else None

    @property
    def sem_dim(self) -> int:
        """训练时并入的语义特征维度（0=无语义，纯第 5 期行为）。"""
        return self._sem_dim

    @property
    def semantic(self) -> object | None:
        """绑定的语义编码器（可能为 None）；供 status 观测。"""
        return self._semantic

    # -- 预测 -----------------------------------------------------------
    def _encode(self, text: str, features: dict | None):
        """把 text + 数值特征 拼成 (TF-IDF, 数值) 稀疏输入；无需义时行为同第 5 期。

        语义列（sem_dim>0，第 6 期）追加在数值列之后，顺序为
        [TF-IDF] + [数值] + [语义512]，与 train_router 训练矩阵严格一致。
        """
        import numpy as np  # noqa: PLC0415
        from scipy.sparse import csr_matrix, hstack  # noqa: PLC0415
        if self._vectorizer is not None:
            x_text = self._vectorizer.transform([text or " "])
        else:
            x_text = csr_matrix((1, 0))
        row = [float((features or {}).get(k, 0.0)) for k in self._feature_keys]
        cols = [x_text, csr_matrix(np.array([row]))]
        if self._sem_dim > 0:
            import numpy as _np  # noqa: PLC0415
            sem = self._semantic.encode(text)
            if sem is None:
                # 语义列不可得（理论上 available 已兜底，防御万一）
                sem = [0.0] * self._sem_dim
            cols.append(csr_matrix(_np.array([sem[:self._sem_dim]])))
        return hstack(cols).tocsr()

    def predict(self, text: str, features: dict | None = None) -> MLPrediction | None:
        """训练产物预测：返回概率最高档；不可用返回 None（静默回落）。"""
        if not self.available:
            return None
        x = self._encode(text, features)
        probs = self._model.predict_proba(x)[0]
        tier = int(probs.argmax())
        return MLPrediction(tier=tier, prob=float(probs[tier]))

    def decide(
        self,
        text: str,
        features: dict | None = None,
        calibration=None,
    ) -> int | None:
        """精判入口：置信门 + 校准偏置后返回档位；不采用返回 None。

        与规则路径复用一个置信门/偏置逻辑（apply_calibration）：
        - 预测最高档概率 < 门（CONFIDENCE_BASE + threshold_adjust）→ 信心不足，
          返回 None，由 route() 保留规则档位；
        - 否则对最高档应用 apply_calibration（偏强档降/偏弱档升，最多一档）。
        硬规则档位由 route() 在调用方排除（decision.hard_rule 时不走精判）。
        """
        if not self.available:
            return None
        pred = self.predict(text, features)
        if pred is None:
            return None
        gate = CONFIDENCE_BASE + (
            calibration.threshold_adjust if calibration is not None else 0.0
        )
        if pred.prob < gate:
            return None
        if calibration is not None:
            return apply_calibration(pred.tier, pred.prob, False, calibration)
        return pred.tier


def load_ml_router(artifact_path: Path | None = None) -> MLRouter:
    """便捷工厂：缺依赖/缺产物时返回一个 available=False 的 MLRouter。"""
    return MLRouter(artifact_path)