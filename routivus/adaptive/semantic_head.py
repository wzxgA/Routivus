"""L3 本地语义头（方案 10 §4.7）：冻结 512 维句向量上的「每档质心 + 温度 softmax」。

定位：**不微调语义编码器**（那是通用模型，本地重训不现实也不该做），只在它的输出
上加一个几 KB 的本地映射。得到的 4 维"档位概率"作为本地训练模型的额外特征列，
于是"语义口味"也能随本机反馈一起进化。

为什么便宜：质心就是每档样本向量的均值；几十条样本即可成形，训练耗时毫秒级。
代价与风险：小样本极易过拟合 —— 因此

- 只喂**带语义向量**的样本（编码器不可用时直接返回 None，不硬凑）；
- 每档至少 ``MIN_PER_TIER`` 条、至少 2 个档位才训练；
- 自带 holdout 准确率，供状态展示与"是否值得用"的判断（不劣门仍由 evolve 把守）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MIN_PER_TIER = 8
TEMPERATURE = 0.1


@dataclass
class SemanticHead:
    """每档质心 + 温度；``probs`` 给 4 维档位概率。"""

    centroids: list[list[float]] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    temperature: float = TEMPERATURE
    dim: int = 0
    train_acc: float | None = None
    holdout_acc: float | None = None

    # -- 序列化 ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "centroids": self.centroids,
            "counts": self.counts,
            "temperature": self.temperature,
            "dim": self.dim,
            "train_acc": self.train_acc,
            "holdout_acc": self.holdout_acc,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SemanticHead | None":
        if not isinstance(data, dict) or not data.get("centroids"):
            return None
        try:
            return cls(
                centroids=[[float(x) for x in row] for row in data["centroids"]],
                counts=[int(x) for x in (data.get("counts") or [])],
                temperature=float(data.get("temperature", TEMPERATURE)),
                dim=int(data.get("dim", 0)),
                train_acc=data.get("train_acc"),
                holdout_acc=data.get("holdout_acc"),
            )
        except (TypeError, ValueError):
            return None

    # -- 推理 -----------------------------------------------------------
    def probs(self, vec: Sequence[float] | None) -> list[float]:
        """余弦相似度 → 温度 softmax；向量缺失时返回均匀分布。"""
        import numpy as np  # noqa: PLC0415

        n = len(self.centroids)
        if n == 0:
            return [0.25, 0.25, 0.25, 0.25]
        if not vec:
            return [1.0 / n] * n
        v = np.asarray(list(vec)[: self.dim] or [0.0] * self.dim, dtype=float)
        nv = float(np.linalg.norm(v)) or 1.0
        logits = []
        for row in self.centroids:
            c = np.asarray(row, dtype=float)
            nc = float(np.linalg.norm(c)) or 1.0
            logits.append(float(v @ c) / (nv * nc))
        z = np.asarray(logits, dtype=float) / max(self.temperature, 1e-6)
        z = z - z.max()
        e = np.exp(z)
        return (e / e.sum()).tolist()

    def features_for(self, samples: Sequence[dict[str, Any]],
                     rows: Sequence[int]) -> list[list[float]]:
        """按行取样本的语义向量 → 4 维档位概率列（缺失向量给均匀分布）。"""
        return [self.probs(samples[i].get("sem")) for i in rows]


# ---------------------------------------------------------------------------
# 训练 / 存取
# ---------------------------------------------------------------------------

def train_head(samples: Sequence[dict[str, Any]], *,
               holdout_ratio: float = 0.2) -> SemanticHead | None:
    """从带语义向量的样本训质心；样本不足/档位不足时返回 None（不硬凑）。"""
    usable = [s for s in samples if s.get("sem")]
    if len(usable) < MIN_PER_TIER * 2:
        return None
    by_tier: dict[int, list[list[float]]] = {}
    for s in usable:
        by_tier.setdefault(int(s["tier"]), []).append(list(s["sem"]))
    if len(by_tier) < 2 or any(len(v) < MIN_PER_TIER for v in by_tier.values()):
        return None

    import numpy as np  # noqa: PLC0415

    tiers = sorted(by_tier)
    centroids = [np.mean(np.asarray(by_tier[t], dtype=float), axis=0) for t in tiers]
    # 按时间切分算 holdout 准确率（与 evolve 的口径一致）
    cut = max(1, int(round(len(usable) * (1.0 - holdout_ratio))))
    head = SemanticHead(
        centroids=[[float(x) for x in c] for c in centroids],
        counts=[len(by_tier[t]) for t in tiers],
        dim=len(centroids[0]),
    )
    head.train_acc = _accuracy(head, tiers, usable[:cut])
    head.holdout_acc = _accuracy(head, tiers, usable[cut:])
    return head


def _accuracy(head: SemanticHead, tiers: list[int],
              subset: Sequence[dict[str, Any]]) -> float | None:
    if not subset:
        return None
    hits = 0
    for s in subset:
        probs = head.probs(s.get("sem"))
        best = tiers[max(range(len(probs)), key=lambda i: probs[i])]
        if best == int(s["tier"]):
            hits += 1
    return hits / len(subset)


def save_head(head: SemanticHead | None, path: Path) -> None:
    """把语义头写进 JSON（几 KB）；None 时删掉旧文件，避免残留误导状态。"""
    from .store import atomic_write_json

    try:
        if head is None:
            path.unlink(missing_ok=True)
            return
        atomic_write_json(path, head.to_dict())
    except Exception:  # noqa: BLE001 - 存不下来只影响展示
        pass


def load_head(path: Path) -> SemanticHead | None:
    from .store import read_json_safe

    return SemanticHead.from_dict(read_json_safe(path, None))
