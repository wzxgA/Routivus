"""SmartRouter ML 精判接入与静默回落。

职责：加载离线训练的产物（tools/train_router.py 生成的 router.lgb），
在规则路由之后、安全后处理之前参与精判——取"概率最高档"，受 置信门 +
校准偏置 约束；任何缺依赖 / 缺产物 / 产物损坏 情形一律**静默回落**，
行为退化为纯规则。

导入保护（主进程零硬性 ML 依赖）：
    joblib / lightgbm / sklearn / numpy 仅在 onnx 运行期 import，
    任一把 import 失败即在初始化时把本模块标记为不可用（available=False），
    主进程绝不因缺依赖而报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import logging

from ..adaptive.calibrate import CONFIDENCE_BASE, apply_calibration

logger = logging.getLogger("routivus.router.ml_router")


def _log_load_failure(path: object, reason: str, exc: BaseException) -> None:
    """产物加载失败要说清"哪个文件、什么原因"（方案 10 §4.6）。

    之前是裸 except：环境里 onnxruntime / lightgbm 装坏时，界面只有一句
    「ML 精判不可用（无产物或依赖缺失）」，日志里什么都看不到。
    """
    logger.warning("ML 产物加载失败（%s）：%s —— %s", reason, path, exc)

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
        # 可选语义编码器（bge 512 维）。训练产物带 sem_dim>0 时，
        # 预测需语义列；语义不可用则该级回落，继续试无语义兜底产物。
        self._semantic = semantic
        self._sem_dim = 0
        # 产物来源（semantic / nosem / explicit / ""）与不可用原因码（方案 10 §4.5/§4.6）
        self._source = ""
        self._unavailable_reason = ""
        # 成功加载的那个产物文件（看板展示体积/时间戳用）；不可用时为 None
        self._artifact_path: Path | None = None
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
        """按**回落链**加载产物（方案 10 §4.5）：

        显式路径 > 语义版（``router.lgb``）> 无语义兜底（``router.lgb.nosem``）。

        为什么要第二级：语义版声明 ``sem_dim>0``，环境里 onnxruntime 缺失/装坏时
        整层 ML 精判会消失；无语义版只用数值特征，那种环境下仍然可用。
        每一级失败都记原因码，全部失败时由 route() 把原因写进 notes。
        """
        chain: list[tuple[str, Path | None]] = []
        if artifact_path is not None:
            chain.append(("explicit", artifact_path))
        else:
            chain.append(("semantic", self._default_path()))
            from ..adaptive.store import ml_router_nosem_path
            chain.append(("nosem", ml_router_nosem_path()))

        reasons: list[str] = []
        for kind, path in chain:
            reason = self._try_load(path)
            if reason == "":
                self._source = kind
                self._unavailable_reason = ""
                self._artifact_path = path
                return
            reasons.append(reason)
        self._source = ""
        self._artifact_path = None
        # 取最后一级的原因：它最接近"为什么最终没有可用产物"
        self._unavailable_reason = reasons[-1] if reasons else "no_artifact"

    def _try_load(self, path: Path | None) -> str:
        """尝试加载一个产物；成功返回空串，失败返回原因码。"""
        if path is None or not path.exists():
            return "no_artifact"
        try:
            import joblib  # noqa: PLC0415
            from sklearn.utils.validation import check_is_fitted  # noqa: PLC0415
        except ImportError:
            # lightgbm / sklearn 不可用（onnxruntime 之外的依赖）
            return "runtime_missing"
        try:
            payload = joblib.load(path)
        except Exception as exc:  # noqa: BLE001 - 损坏 / 缺 lightgbm / 版本不兼容
            _log_load_failure(path, "load_failed", exc)
            return "load_failed"
        try:
            if not isinstance(payload, dict):
                return "bad_artifact"
            if payload.get("format") != ARTIFACT_FORMAT:
                return "version_mismatch"
            model = payload.get("model")
            if not hasattr(model, "predict_proba"):
                return "bad_artifact"
            check_is_fitted(model)
            sem_dim = payload.get("sem_dim", 0) or 0
            # 语义版要求编码器可用；不可用则交给下一级（无语义兜底）
            if sem_dim > 0 and (self._semantic is None or not self._semantic.available):
                return "no_semantic"
        except Exception as exc:  # noqa: BLE001
            _log_load_failure(path, "bad_artifact", exc)
            return "bad_artifact"
        self._vectorizer = payload.get("vectorizer")
        self._model = model
        self._feature_keys = tuple(payload.get("feature_keys") or ())
        self._sem_dim = int(sem_dim)
        self._payload = payload
        return ""

    def _default_path(self) -> Path | None:
        from ..adaptive.store import data_dir
        return data_dir() / _DEFAULT_ARTIFACT

    @property
    def available(self) -> bool:
        """产物已成功加载且模型可用。"""
        return self._model is not None

    @property
    def source(self) -> str:
        """当前产物来源：``semantic`` / ``nosem`` / ``explicit`` / ``""``（不可用）。"""
        return self._source

    @property
    def unavailable_reason(self) -> str:
        """不可用原因码；可用时为空串。供 notes / status / 日志解释"为什么没走 ML"。"""
        return self._unavailable_reason

    @property
    def artifact_path(self) -> Path | None:
        """当前成功加载的产物路径；不可用时为 None（看板用）。"""
        return Path(self._artifact_path) if self._artifact_path else None

    @property
    def n_samples(self) -> int | None:
        return self._payload.get("n_samples") if self._payload else None

    # 以下三个是产物内嵌的展示字段（看板用）：训练时间 / 训练时的验证准确率 /
    # 本地语义头列宽。都只读 payload，缺失一律 None（老产物没有这些键）。
    @property
    def trained_at(self) -> float | None:
        value = self._payload.get("trained_at") if self._payload else None
        return float(value) if value is not None else None

    @property
    def val_accuracy(self) -> float | None:
        value = self._payload.get("val_accuracy") if self._payload else None
        return float(value) if value is not None else None

    @property
    def head_dim(self) -> int:
        value = self._payload.get("head_dim", 0) if self._payload else 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @property
    def sem_dim(self) -> int:
        """训练时并入的语义特征维度（0=无语义）。"""
        return self._sem_dim

    @property
    def semantic(self) -> object | None:
        """绑定的语义编码器（可能为 None）；供 status 观测。"""
        return self._semantic

    # -- 预测 -----------------------------------------------------------
    def _encode(self, text: str, features: dict | None):
        """把 text + 数值特征 拼成 (TF-IDF, 数值) 稀疏输入；无语义时即为纯规则列组合。

        语义列（sem_dim>0）追加在数值列之后，顺序为
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
        notes: list[str] | None = None,
    ) -> int | None:
        """精判入口：置信门 + 校准偏置后返回档位；不采用返回 None。

        与规则路径复用一个置信门/偏置逻辑（apply_calibration）：
        - 预测最高档概率 < 门（CONFIDENCE_BASE + threshold_adjust）→ 信心不足，
          返回 None，由 route() 保留规则档位；
        - 否则对最高档应用 apply_calibration（偏强档降/偏弱档升，最多一档）。
        硬规则档位由 route() 在调用方排除（decision.hard_rule 时不走精判）。

        ``notes``（可选，方案 08 §4.1）记录"精判是否被采用/为何没采用"，
        纯记录，不参与判断。
        """
        if not self.available:
            if notes is not None:
                reason = self._unavailable_reason
                notes.append(f"ml:unavailable:{reason}" if reason else "ml:unavailable")
            return None
        pred = self.predict(text, features)
        if pred is None:
            if notes is not None:
                notes.append("ml:error")
            return None
        gate = CONFIDENCE_BASE + (
            calibration.threshold_adjust if calibration is not None else 0.0
        )
        if pred.prob < gate:
            if notes is not None:
                notes.append(f"ml:skipped:low_conf(p={pred.prob:.2f})")
            return None
        if notes is not None:
            notes.append(f"ml:idx={pred.tier},p={pred.prob:.2f}")
        if calibration is not None:
            calibrated = apply_calibration(pred.tier, pred.prob, False, calibration)
            if calibrated != pred.tier and notes is not None:
                notes.append(f"calibration:{calibrated - pred.tier:+d}")
            return calibrated
        return pred.tier


def load_ml_router(artifact_path: Path | None = None) -> MLRouter:
    """便捷工厂：缺依赖/缺产物时返回一个 available=False 的 MLRouter。"""
    return MLRouter(artifact_path)