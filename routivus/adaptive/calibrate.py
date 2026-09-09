"""校准聚合与应用（phase-03 步骤 C）。

聚合 feedback.log 的隐式信号 → 每档偏置 per_class_bias 与全局阈值调整
threshold_adjust，硬夹紧（±0.15 / ±0.1），单档加权样本不足 20 不校准。

设计依据：ADAPTIVE_ROUTING §8.1（聚合公式）、§6.5（置信门应用）。
与文档的两处有意落地决策（文档 §6.5 示意为 ML 路由的"回落默认档"，
本项目为纯规则路由，按方向注释落地为对称升降一档）：
1. 偏置方向：bias[t] < 0（该档 upgrade 信号多、偏弱）→ 置信门不通过的
   边界输入升一档；bias[t] > 0（downgrade 多、偏强）→ 降一档。
2. 置信门为对称双门：文档门公式只对升档方向生效（bias>0 时 gate_conf
   恒不低于门），此处按"偏强档对称降档"补齐另一方向。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .feedback import read_feedback
from .store import atomic_write_json, calibration_path, read_json_safe

# 档位名（与 xg.router.model_tiers.TIER_NAMES 一致；本地定义避免包环）
TIER_NAMES: tuple[str, ...] = ("Basic", "Enhanced", "Superior", "Ultimate")

MIN_SAMPLES_PER_TIER = 20   # 单档加权样本不足时不校准
MAX_BIAS = 0.15             # per_class_bias 硬夹紧
MAX_THRESHOLD_ADJUST = 0.1  # threshold_adjust 硬夹紧
CONFIDENCE_BASE = 0.5       # 置信门基础阈值


@dataclass(frozen=True)
class Calibration:
    """一次校准的聚合结果。bias/sample 按档位索引 0..3 排列。"""

    bias: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    threshold_adjust: float = 0.0
    samples: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # 加权样本量
    total: float = 0.0

    def bias_of(self, tier_idx: int) -> float:
        return self.bias[tier_idx] if 0 <= tier_idx < 4 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bias": dict(zip(TIER_NAMES, self.bias)),
            "threshold_adjust": self.threshold_adjust,
            "samples": dict(zip(TIER_NAMES, self.samples)),
            "total": self.total,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> Calibration:
        """从持久化 JSON 恢复；结构异常时回退空校准（不抛错）。"""
        if not isinstance(data, dict):
            return Calibration()
        try:
            bias_map = data.get("bias") or {}
            samples_map = data.get("samples") or {}
            bias = tuple(float(bias_map.get(name, 0.0)) for name in TIER_NAMES)
            samples = tuple(float(samples_map.get(name, 0.0)) for name in TIER_NAMES)
            return Calibration(
                bias=bias,  # type: ignore[arg-type]
                threshold_adjust=float(data.get("threshold_adjust", 0.0)),
                samples=samples,  # type: ignore[arg-type]
                total=float(data.get("total", 0.0)),
            )
        except (TypeError, ValueError):
            return Calibration()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def aggregate(
    records: Sequence[dict[str, Any]],
    tier_names: Sequence[str] = TIER_NAMES,
    min_samples: float = MIN_SAMPLES_PER_TIER,
) -> Calibration:
    """按 §8.1 公式聚合反馈记录。

    up[t]/down[t] 为该档 upgrade/downgrade 信号的加权和（weight 求和）；
    n[t] = up+down；n < min_samples → bias=0；否则
    r[t]=(up-down)/n，bias[t]=clamp(-r[t]*0.15, ±0.15)。
    全局 threshold_adjust=clamp(r_global*0.1, ±0.1)。
    未知档位/信号方向的记录跳过不计。
    """
    names = list(tier_names)
    up = [0.0] * len(names)
    down = [0.0] * len(names)
    for rec in records:
        try:
            tier = rec.get("model_tier")
            if tier not in names:
                continue
            idx = names.index(tier)
            weight = float(rec.get("weight", 0.0))
            direction = rec.get("signal")
        except (TypeError, ValueError):
            continue
        if direction == "upgrade":
            up[idx] += weight
        elif direction == "downgrade":
            down[idx] += weight

    bias: list[float] = []
    samples: list[float] = []
    for i in range(len(names)):
        n = up[i] + down[i]
        samples.append(n)
        if n < min_samples:
            bias.append(0.0)
        else:
            r = (up[i] - down[i]) / n
            bias.append(_clamp(-r * MAX_BIAS, -MAX_BIAS, MAX_BIAS))

    total = sum(up) + sum(down)
    if total > 0:
        r_global = (sum(up) - sum(down)) / total
        threshold_adjust = _clamp(r_global * MAX_THRESHOLD_ADJUST,
                                  -MAX_THRESHOLD_ADJUST, MAX_THRESHOLD_ADJUST)
    else:
        threshold_adjust = 0.0
    return Calibration(
        bias=tuple(bias),  # type: ignore[arg-type]
        threshold_adjust=threshold_adjust,
        samples=tuple(samples),  # type: ignore[arg-type]
        total=total,
    )


def save_calibration(calibration: Calibration, path=None) -> None:
    """原子写 calibration.json（tmp+os.replace）。"""
    from .store import ensure_dir

    p = path or calibration_path()
    if path is None:
        ensure_dir()
    atomic_write_json(p, calibration.to_dict())


def load_calibration(path=None) -> Calibration:
    """读取 calibration.json；缺失/损坏回退空校准，绝不抛错。"""
    return Calibration.from_dict(read_json_safe(path or calibration_path()))


def recalibrate(log_path=None, cal_path=None) -> Calibration:
    """读 feedback.log → 聚合 → 落盘 calibration.json → 返回结果。

    无记录时返回空校准且不写盘（保证"删掉 ~/.xg/adaptive/ 即回到
    第 1 期行为"的验收：不重建目录）。
    """
    records = read_feedback(log_path)
    if not records:
        return Calibration()
    calibration = aggregate(records)
    save_calibration(calibration, cal_path)
    return calibration


def apply_calibration(
    tier_idx: int,
    confidence: float,
    hard_rule: bool,
    calibration: Calibration,
) -> int:
    """置信门 + 档位偏置：返回校准后的档位索引。

    只读纯函数，供 route() 在规则打分后、安全后处理前调用：
    - 硬规则决策（confidence=1.0 的安全兜底）不受校准影响；
    - bias == 0（样本不足或恰好均衡）不动；
    - 对称双置信门（§6.5 的门公式按方向注释落地为双向）：
      偏弱档（bias<0）：confidence + bias < 0.5 + threshold_adjust 时升一档；
      偏强档（bias>0）：confidence - bias < 0.5 + threshold_adjust 时降一档。
      即 |bias| 把置信门的适用窗口向两侧对称放宽，threshold_adjust 整体平移。
    单次最多移动一档，夹在 0..3，不会跳档。
    """
    if hard_rule:
        return tier_idx
    bias = calibration.bias_of(tier_idx)
    if bias == 0.0:
        return tier_idx
    gate = CONFIDENCE_BASE + calibration.threshold_adjust
    if bias < 0:
        if confidence + bias < gate:
            return min(tier_idx + 1, 3)
    else:
        if confidence - bias < gate:
            return max(tier_idx - 1, 0)
    return tier_idx
