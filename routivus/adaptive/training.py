"""SmartRouter 训练核心（可复用）。

从 ``tools/train_router.py`` 抽出来的**离线训练内核**，供三处共用：

- ``tools/train_router.py``（人工触发的 CLI，产物写数据目录）
- ``routivus/adaptive/evolve.py``（本地自动进化，方案 10 §4.2）
- ``tools/distill_nosem_router.py``（出厂「无语义兜底」产物的规则蒸馏）

三件事在这里，且只有这里：

1. **样本构建**（纯逻辑，无 ML 依赖，可单测）：标注数据 + feedback 记录 → 训练样本；
   标签反推（``tier ± 1``，到顶/到底丢弃）、去噪（硬规则剔除、同 text_hash 加权投票、
   平票丢弃）。
2. **训练与落盘**：``[TF-IDF] + [数值] + [语义]`` 三段列序（顺序即契约，预测端
   ``router/ml_router.py`` 按同样顺序拼列）。
3. **产物评估**：给定产物 + 样本集算加权准确率，供进化做「不劣于当前产物」的硬门
   （方案 10 §4.2）。

语义列的两种来源（互斥，优先编码器）：

- **编码器**：样本有原文（标注训练 / 出厂蒸馏）→ 调 ``semantic.encode(text)``；
- **预存向量**：本地样本无原文（隐私要求，方案 10 §4.1）→ 用样本自带的 ``sem`` 字段
  （采集时编码，int8 落盘）。

ML 依赖（joblib / lightgbm / sklearn / scipy / numpy）只在函数内 import，主进程
不因缺依赖报错。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Sequence

TIER_NAMES = ("Basic", "Enhanced", "Superior", "Ultimate")

# features.extract() 的数值特征列（feedback 样本的可用信号）
FEATURE_KEYS = (
    "len_chars", "len_words", "num_code_blocks", "code_chars_ratio",
    "num_json", "num_xml", "num_lists", "has_attachment", "question_mark",
    "is_chatty", "num_chatty_kw", "num_debug_kw", "num_risk_kw",
    "num_planning_kw", "num_arch_kw", "num_teach_kw", "num_impl_kw",
    "num_review_kw",
)

ARTIFACT_FORMAT = 1  # 产物结构版本号


# ---------------------------------------------------------------------------
# 样本构建（纯逻辑，无 ML 依赖）
# ---------------------------------------------------------------------------

def tier_to_idx(tier: str | int | None) -> int | None:
    """档位名/索引 → 0..3；无法识别返回 None。"""
    if tier is None:
        return None
    if isinstance(tier, int):
        return tier if 0 <= tier <= 3 else None
    try:
        return TIER_NAMES.index(tier)
    except ValueError:
        return None


def derive_label(tier_idx: int, direction: str) -> int | None:
    """反推标签：tier_idx ± 1；到顶/到底且方向无意义时返回 None（丢弃）。"""
    if direction == "upgrade":
        return tier_idx + 1 if tier_idx < 3 else None
    if direction == "downgrade":
        return tier_idx - 1 if tier_idx > 0 else None
    return None


def _features_hit_hard_rule(features: dict[str, Any] | None) -> bool:
    """feedback 记录是否命中硬规则（risk / chatty）：档位被安全闸锁定，剔除。"""
    if not isinstance(features, dict):
        return False
    if features.get("num_risk_kw", 0) and features["num_risk_kw"] > 0:
        return True
    if features.get("is_chatty") == 1:
        return True
    return False


def aggregate_by_hash(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """同 text_hash 的多条记录聚合为一条：加权投票定方向，平票/全无方向丢弃。

    无 text_hash 的记录原样保留（逐条处理）。
    """
    by_hash: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for rec in records:
        h = rec.get("text_hash") or ""
        if h:
            by_hash.setdefault(h, []).append(rec)
        else:
            passthrough.append(rec)

    aggregated: list[dict[str, Any]] = []
    for recs in by_hash.values():
        up = sum(r.get("weight", 0.0) for r in recs if r.get("signal") == "upgrade")
        down = sum(r.get("weight", 0.0) for r in recs if r.get("signal") == "downgrade")
        if up > down:
            signal, weight = "upgrade", up
        elif down > up:
            signal, weight = "downgrade", down
        else:
            continue  # 平票或无方向：自相矛盾的噪声，丢弃
        # 取第一条的 tier/features 做载体
        aggregated.append({**recs[0], "signal": signal, "weight": weight})
    return passthrough + aggregated


def build_samples(
    labeled_path: Path | None,
    feedback_records: Sequence[dict[str, Any]],
    sem_by_hash: dict[str, list[float]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建训练样本，返回 (samples, stats)。

    sample 结构：``{"text", "tier", "weight", "features", "sem", "text_hash"}``

    - 标注样本：``text + tier`` 来自人工标注，``weight=1.0``；
    - feedback 样本：无原文（``text=""``），tier 反推，weight = 信号聚合权重；
      ``sem_by_hash`` 命中时带上采集期存下的语义向量（方案 10 §4.1）。

    ``stats`` 报告各阶段丢弃计数（去噪透明度）。
    """
    sem_by_hash = sem_by_hash or {}
    samples: list[dict[str, Any]] = []
    stats = {"labeled": 0, "feedback": 0, "dropped_hard_rule": 0,
             "dropped_no_label": 0, "dropped_conflict": 0, "with_sem": 0}

    if labeled_path is not None and labeled_path.exists():
        with open(labeled_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    stats["dropped_no_label"] += 1
                    continue
                idx = tier_to_idx(rec.get("tier"))
                if idx is None or not rec.get("text"):
                    stats["dropped_no_label"] += 1
                    continue
                samples.append({
                    "text": rec["text"], "tier": idx, "weight": 1.0,
                    "features": rec.get("features"), "sem": rec.get("sem"),
                    "text_hash": rec.get("text_hash", ""),
                })
                stats["labeled"] += 1

    for rec in aggregate_by_hash(feedback_records):
        if _features_hit_hard_rule(rec.get("features")):
            stats["dropped_hard_rule"] += 1
            continue
        tier_idx = tier_to_idx(rec.get("model_tier"))
        label = derive_label(tier_idx, rec.get("signal", "")) \
            if tier_idx is not None else None
        if label is None:
            stats["dropped_no_label"] += 1
            continue
        text_hash = rec.get("text_hash") or ""
        sem = sem_by_hash.get(text_hash)
        if sem:
            stats["with_sem"] += 1
        samples.append({
            "text": "", "tier": label,
            "weight": float(rec.get("weight", 0.0)) or 0.3,
            "features": rec.get("features"), "sem": sem,
            "text_hash": text_hash,
        })
        stats["feedback"] += 1

    return samples, stats


# ---------------------------------------------------------------------------
# 特征矩阵（三段列序的唯一实现）
# ---------------------------------------------------------------------------

def _sem_source(samples: Sequence[dict[str, Any]], semantic, has_text: bool) -> str:
    """决定语义列来源：``encoder``（有原文 + 编码器可用）/ ``samples`` / ``none``。"""
    if semantic is not None and getattr(semantic, "available", False) and has_text:
        return "encoder"
    if any(s.get("sem") for s in samples):
        return "samples"
    return "none"


def build_matrix(
    samples: Sequence[dict[str, Any]],
    *,
    semantic=None,
    vectorizer=None,
    rows: Sequence[int] | None = None,
    head=None,
    include_sem: bool = True,
) -> tuple[Any, Any, int]:
    """构造 ``[TF-IDF] + [数值] + [语义]`` 稀疏矩阵。

    返回 ``(x, vectorizer, sem_dim)``。``vectorizer`` 传入时用它 transform（评估 /
    追加训练），为 None 时 fit（首次训练）。

    ``rows`` 指定参与的行（训练/验证切分复用同一个 vectorizer 时必须显式传行号，
    否则 fit 会看到验证集造成泄漏）。
    """
    import numpy as np  # noqa: PLC0415
    from scipy.sparse import csr_matrix, hstack  # noqa: PLC0415
    from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415

    idx = list(range(len(samples))) if rows is None else list(rows)
    texts = [samples[i]["text"] for i in idx]
    has_text = any(t.strip() for t in texts)

    if has_text:
        if vectorizer is None:
            vectorizer = TfidfVectorizer(
                analyzer="char", ngram_range=(1, 2), max_features=30000,
                min_df=1, token_pattern=None,
            )
            x_text = vectorizer.fit_transform(texts)
        else:
            x_text = vectorizer.transform(texts)
    else:
        vectorizer = None
        x_text = csr_matrix((len(idx), 0))

    feats = np.array([[float((samples[i].get("features") or {}).get(k, 0.0))
                       for k in FEATURE_KEYS] for i in idx], dtype=float)
    cols = [x_text, csr_matrix(feats)]

    source = _sem_source(samples, semantic, has_text) if include_sem else "none"
    sem_dim = 0
    if source == "encoder":
        sem_dim = int(semantic.dim)
        rows_sem = np.zeros((len(idx), sem_dim), dtype=float)
        for pos, i in enumerate(idx):
            vec = semantic.encode(samples[i]["text"])
            if vec is not None:
                rows_sem[pos] = vec[:sem_dim]
        cols.append(csr_matrix(rows_sem))
    elif source == "samples":
        sem_dim = len(next(s["sem"] for s in samples if s.get("sem")))
        rows_sem = np.zeros((len(idx), sem_dim), dtype=float)
        for pos, i in enumerate(idx):
            vec = samples[i].get("sem")
            if vec:
                rows_sem[pos] = vec[:sem_dim]
        cols.append(csr_matrix(rows_sem))

    if head is not None:
        cols.append(csr_matrix(np.asarray(head.features_for(samples, idx), dtype=float)))

    return hstack(cols).tocsr(), vectorizer, sem_dim


# ---------------------------------------------------------------------------
# 训练与落盘
# ---------------------------------------------------------------------------

def train_and_save(
    samples: Sequence[dict[str, Any]],
    out_path: Path,
    val_size: float = 0.2,
    n_estimators: int = 200,
    seed: int = 42,
    semantic=None,
    head=None,
) -> dict[str, Any]:
    """TF-IDF + LightGBM 训练并落盘产物（joblib 单文件容器）。

    ``semantic``：编码器可用且有原文时并入 512 维语义列（出厂蒸馏 / 标注训练）；
    否则退到样本自带的 ``sem``（本地样本库，方案 10 §4.1）。

    ``head``（可选，方案 10 §4.7）：本地语义头，其 4 维档位概率追加在语义列之后。

    返回训练报告 dict（样本量/验证准确率/产物大小/语义列维度）。
    """
    try:
        import joblib  # noqa: PLC0415
        import lightgbm as lgb  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        from sklearn.model_selection import train_test_split  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 依赖缺失走友好提示
        raise SystemExit(
            f"缺少 ML 依赖：{exc}\n请先安装：pip install -e . （ML 依赖已并入项目核心）"
        ) from exc

    if len(samples) < 2:  # LightGBM 分类器最少需要 2 条样本
        raise SystemExit(
            f"样本量不足（{len(samples)} < 2）：请补充标注数据或积累 feedback 后重试"
        )

    y = np.array([s["tier"] for s in samples], dtype=int)
    w = np.array([s["weight"] for s in samples], dtype=float)

    # 训练/验证划分（有标注文本时分层，纯 feedback 时退化为普通划分）
    n_classes = len(set(y.tolist()))
    stratify = y if n_classes > 1 and min(np.bincount(y)) >= 2 else None
    split = len(samples) >= 10
    if split:
        idx = np.arange(len(samples))
        tr, va = train_test_split(
            idx, test_size=val_size, random_state=seed, stratify=stratify,
        )
    else:  # 样本太少不切分（验证准确率记为 None）
        tr = va = np.arange(len(samples))

    # TF-IDF 只在训练行上 fit，再用同一个 vectorizer transform 验证行（防泄漏）
    x_tr, vectorizer, sem_dim = build_matrix(
        samples, semantic=semantic, rows=list(tr), head=head,
    )
    x_va, _, _ = build_matrix(
        samples, semantic=semantic, vectorizer=vectorizer, rows=list(va), head=head,
    )

    model = lgb.LGBMClassifier(
        objective="multiclass", num_class=4, n_estimators=n_estimators,
        random_state=seed, verbose=-1,
    )
    model.fit(x_tr, y[tr], sample_weight=w[tr])

    acc = None
    if split and len(va) and len(set(y[va].tolist())) > 1:
        acc = float((model.predict(x_va) == y[va]).mean())

    payload = {
        "format": ARTIFACT_FORMAT,
        "vectorizer": vectorizer,
        "model": model,
        "feature_keys": list(FEATURE_KEYS),
        "trained_at": time.time(),
        "n_samples": len(samples),
        "val_accuracy": acc,
        "sem_dim": sem_dim,  # 绑定语义列宽，0=无语义
        # 本地语义头（方案 10 §4.7）：把 head 本体一起装进产物，评估/预测才能
        # 自洽复现那几列，而不是补零（补零会让比较失公平）。
        # 列宽取质心个数（=参与训练的档位数），不要写死 4 —— 档位不全时列宽更小。
        "head": head.to_dict() if head is not None else None,
        "head_dim": len(head.centroids) if head is not None else 0,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, out_path)
    return {
        "n_samples": len(samples), "n_train": len(tr), "n_val": len(va),
        "val_accuracy": acc, "artifact_bytes": out_path.stat().st_size,
        "sem_dim": sem_dim,
    }


# ---------------------------------------------------------------------------
# 产物评估（进化用的硬门）
# ---------------------------------------------------------------------------

def load_artifact(path: Path) -> dict[str, Any] | None:
    """读产物；损坏/格式不符返回 None（绝不抛错）。"""
    try:
        if not path.exists():
            return None
        import joblib  # noqa: PLC0415
        payload = joblib.load(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
        return None
    return payload


def artifact_accuracy(
    payload: dict[str, Any] | None,
    samples: Sequence[dict[str, Any]],
) -> float | None:
    """产物在给定样本集上的加权准确率；不可评估返回 None。

    样本无原文时 TF-IDF 列全零（本地样本即如此），因此**比较只在本地产物之间公平**
    （方案 10 §4.2 的实现注记）：出厂产物带文本训练，在无原文的 holdout 上天然吃亏。
    """
    if payload is None or not samples:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        model = payload.get("model")
        if model is None or not hasattr(model, "predict_proba"):
            return None
        sem_dim = int(payload.get("sem_dim", 0) or 0)
        need = int(payload.get("head_dim", 0) or 0)
        stored_head = None
        if need:
            from .semantic_head import SemanticHead  # noqa: PLC0415

            stored_head = SemanticHead.from_dict(payload.get("head"))
            if stored_head is not None:
                need = len(stored_head.centroids)  # 以头本体的列宽为准
        # 语义列：产物要 dim 维，样本里只有采集时存的那份
        # include_sem=False：语义列由这里按产物声明的列宽显式拼接，
        # 否则 build_matrix 会按样本自带向量再拼一次（列数翻倍 → 评估报废）
        x, _, _ = build_matrix(
            samples, semantic=None, vectorizer=payload.get("vectorizer"),
            include_sem=False,
        )
        cols = [x]
        if sem_dim > 0:
            from scipy.sparse import csr_matrix  # noqa: PLC0415
            rows = np.zeros((len(samples), sem_dim), dtype=float)
            for i, s in enumerate(samples):
                vec = s.get("sem")
                if vec:
                    rows[i] = vec[:sem_dim]
            cols.append(csr_matrix(rows))
        if need:
            # 带语义头的产物：用产物自带的那份头复现那几列；产物里没有（旧格式）
            # 才补零列 —— 补零会让比较不公平，但至少列宽一致、不会报错
            from scipy.sparse import csr_matrix  # noqa: PLC0415

            if stored_head is not None:
                cols.append(csr_matrix(np.asarray(
                    stored_head.features_for(samples, list(range(len(samples)))),
                    dtype=float,
                )))
            else:
                cols.append(csr_matrix(np.zeros((len(samples), need), dtype=float)))
        from scipy.sparse import hstack  # noqa: PLC0415
        x_eval = hstack(cols).tocsr()
        pred = model.predict(x_eval)
        y = np.array([s["tier"] for s in samples], dtype=int)
        w = np.array([float(s.get("weight", 1.0)) for s in samples], dtype=float)
        return float((w * (pred == y)).sum() / w.sum())
    except Exception:  # noqa: BLE001
        return None


def holdout_split(
    samples: Sequence[dict[str, Any]], holdout_ratio: float = 0.2,
) -> tuple[list[int], list[int]]:
    """按**时间**切分（不随机，避免时间泄漏，方案 10 §4.2）：最近 20% 作 holdout。"""
    n = len(samples)
    if n < 10:
        return list(range(n)), []
    cut = max(1, int(round(n * (1.0 - holdout_ratio))))
    return list(range(cut)), list(range(cut, n))
