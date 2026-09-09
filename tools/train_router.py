"""SmartRouter 离线训练脚本（第 5 期 B1）。

标注数据 + feedback.log → TF-IDF + LightGBM，产物写 ~/.xg/adaptive/router.lgb。

用法：
    python tools/train_router.py labeled.jsonl            # 标注 + feedback 混合训练
    python tools/train_router.py --feedback-only          # 仅 feedback.log（无 TF-IDF 词信号）
    python tools/train_router.py labeled.jsonl --out m.bin

依赖（已随项目核心依赖安装，主进程仅离线脚本使用）：
    pip install -e .

样本来源与去噪（设计依据 phase-05 文档 + 反推标签规则）：
- 标注数据 JSONL，每行 {"text": "...", "tier": 0..3 或档位名}，权重 1.0；
- feedback.log 无原文（只有 text_hash + features 快照），标签由
  "当时档位 ± 信号方向" 反推：label = clamp(tier_idx + Δ)，Δ=±1；
- 噪声处理：到顶 upgrade / 到底 downgrade 丢弃（无区分度）；同 text_hash
  先聚合方向（加权投票，平票丢弃）；命中硬规则（risk / chatty 特征）的
  记录剔除（档位被安全闸锁定，与难度判断无关）；
- feedback 样本无原文 → TF-IDF 列全零，仅靠数值特征参与。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # 脚本独立可跑：保证能 import routivus

from routivus.adaptive.feedback import read_feedback  # noqa: E402

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
# 样本构建（纯逻辑，无 ML 依赖，可单测）
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
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """构建训练样本，返回 (samples, stats)。

    sample 结构：{"text": str, "tier": 0..3, "weight": float, "features": dict|None}
    - 标注样本：text + tier 来自人工标注，weight=1.0；
    - feedback 样本：无原文（text=""），tier 反推，weight=信号聚合权重；
    stats 报告各阶段丢弃计数（去噪透明度）。
    """
    samples: list[dict[str, Any]] = []
    stats = {"labeled": 0, "feedback": 0, "dropped_hard_rule": 0,
             "dropped_no_label": 0, "dropped_conflict": 0}

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
                    "features": None,
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
        samples.append({
            "text": "", "tier": label,
            "weight": float(rec.get("weight", 0.0)) or 0.3,
            "features": rec.get("features"),
        })
        stats["feedback"] += 1

    return samples, stats


# ---------------------------------------------------------------------------
# 训练与产物（ML 依赖仅在此段 import）
# ---------------------------------------------------------------------------

def train_and_save(
    samples: Sequence[dict[str, Any]],
    out_path: Path,
    val_size: float = 0.2,
    n_estimators: int = 200,
    seed: int = 42,
    semantic=None,
) -> dict[str, Any]:
    """TF-IDF + LightGBM 训练并落盘产物（joblib 单文件容器）。

    ``semantic``（router.semantic.SemanticEncoder）可选，第 6 期 C2：
    提供且可用时把每个有原文样本编码成 512 维语义向量并入特征矩阵
    （[TF-IDF] + [数值] + [语义512]），并在产物写入 ``sem_dim``；
    预测端 ml_router 以相同列序拼语义列。无用例时行为与第 5 期完全一致。

    返回训练报告 dict（样本量/验证准确率/产物大小/语义列维度）。
    """
    try:
        import joblib
        import lightgbm as lgb
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.model_selection import train_test_split
    except ImportError as exc:  # pragma: no cover - 依赖缺失走友好提示
        raise SystemExit(
            f"缺少 ML 依赖：{exc}\n请先安装：pip install -e . （ML 依赖已并入项目核心）"
        ) from exc

    if len(samples) < 2:  # LightGBM 分类器最少需要 2 条样本
        raise SystemExit(
            f"样本量不足（{len(samples)} < 2）：请补充标注数据或积累 feedback 后重试"
        )

    texts = [s["text"] for s in samples]
    y = np.array([s["tier"] for s in samples], dtype=int)
    w = np.array([s["weight"] for s in samples], dtype=float)
    feats = np.array([[float((s.get("features") or {}).get(k, 0.0))
                       for k in FEATURE_KEYS] for s in samples], dtype=float)

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

    # TF-IDF：中文无空格分词，用字符级 n-gram。
    # 纯 feedback 训练时无原文（全空文本），TF-IDF 列退化为零宽矩阵。
    has_text = any(t.strip() for t in texts)
    from scipy.sparse import hstack, csr_matrix  # noqa: PLC0415
    if has_text:
        vectorizer: Any = TfidfVectorizer(
            analyzer="char", ngram_range=(1, 2), max_features=30000,
            min_df=1, token_pattern=None,
        )
        x_text_tr = vectorizer.fit_transform([texts[i] for i in tr])
        x_text_va = vectorizer.transform([texts[i] for i in va])
    else:
        vectorizer = None
        x_text_tr = csr_matrix((len(tr), 0))
        x_text_va = csr_matrix((len(va), 0))

    # 语义列（第 6 期 C2）：有原文的样本拼 512 维编码，feedback 无原文用零行。
    sem_dim = 0
    if semantic is not None and semantic.available and has_text:
        sem_dim = semantic.dim
        sem_rows_tr = np.zeros((len(tr), sem_dim), dtype=float)
        sem_rows_va = np.zeros((len(va), sem_dim), dtype=float)
        for i in tr:
            vec = semantic.encode(texts[i])
            if vec is not None:
                sem_rows_tr[list(tr).index(i)] = vec[:sem_dim]
        for i in va:
            vec = semantic.encode(texts[i])
            if vec is not None:
                sem_rows_va[list(va).index(i)] = vec[:sem_dim]
        x_sem_tr = csr_matrix(sem_rows_tr)
        x_sem_va = csr_matrix(sem_rows_va)
        x_tr = hstack([x_text_tr, csr_matrix(feats[tr]),
                       x_sem_tr]).tocsr()
        x_va = hstack([x_text_va, csr_matrix(feats[va]),
                       x_sem_va]).tocsr()
    else:
        x_tr = hstack([x_text_tr, csr_matrix(feats[tr])]).tocsr()
        x_va = hstack([x_text_va, csr_matrix(feats[va])]).tocsr()

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
        "sem_dim": sem_dim,  # 第 6 期 C2：绑定语义列宽，0=无语义
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, out_path)
    return {
        "n_samples": len(samples), "n_train": len(tr), "n_val": len(va),
        "val_accuracy": acc, "artifact_bytes": out_path.stat().st_size,
        "sem_dim": sem_dim,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SmartRouter 离线训练：标注数据 + feedback.log → router.lgb",
    )
    parser.add_argument(
        "labeled", nargs="?", default=None,
        help="人工标注 JSONL 路径，每行 {\"text\": ..., \"tier\": 0..3 或档位名}",
    )
    parser.add_argument(
        "--feedback-only", action="store_true",
        help="仅用 feedback.log 训练（无标注数据，TF-IDF 列全零）",
    )
    parser.add_argument(
        "--out", default=None,
        help="产物路径，默认 ~/.xg/adaptive/router.lgb",
    )
    parser.add_argument("--val-size", type=float, default=0.2)
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument(
        "--semantic-onnx", default=None,
        help="bge 语义编码器产物 .onnx 路径（可选，第 6 期）；提供时并入 512 维语义列",
    )
    args = parser.parse_args(argv)

    if not args.feedback_only and not args.labeled:
        parser.error("需要标注数据路径，或使用 --feedback-only")

    out_path = Path(args.out) if args.out else _repo_default_artifact_path()

    labeled_path = Path(args.labeled) if args.labeled else None
    if labeled_path is not None and not labeled_path.exists():
        print(f"错误：标注文件不存在：{labeled_path}", file=sys.stderr)
        return 1

    feedback_records = read_feedback()
    samples, stats = build_samples(labeled_path, feedback_records)
    print(f"样本：标注 {stats['labeled']} 条 + feedback {stats['feedback']} 条；"
          f"丢弃（硬规则 {stats['dropped_hard_rule']} / 无标签或越界 "
          f"{stats['dropped_no_label']}）")
    if not samples:
        print("错误：无可用训练样本（feedback.log 为空且无标注数据）",
              file=sys.stderr)
        return 1

    # 第 6 期 C2：可选语义编码器（--semantic-onnx），可就可用性打印提示
    semantic = None
    if args.semantic_onnx:
        from routivus.router.semantic import SemanticEncoder  # noqa: PLC0415
        semantic = SemanticEncoder(Path(args.semantic_onnx))
        if not semantic.available:
            print("提示：语义编码器不可用（产物/依赖缺失），本次训练不含语义列",
                  file=sys.stderr)

    report = train_and_save(
        samples, out_path, val_size=args.val_size,
        n_estimators=args.n_estimators, semantic=semantic,
    )
    acc = f"{report['val_accuracy']:.3f}" if report["val_accuracy"] is not None else "N/A"
    sem_note = f"，语义列 {report['sem_dim']} 维" if report["sem_dim"] else ""
    print(f"训练完成：{report['n_samples']} 样本"
          f"（训练 {report['n_train']} / 验证 {report['n_val']}），"
          f"验证准确率 {acc}{sem_note}")
    print(f"产物：{out_path}（{report['artifact_bytes'] / 1024:.0f} KB）")
    return 0


def _repo_default_artifact_path() -> Path:
    """默认产物路径：用户数据目录 ~/.xg/adaptive/router.lgb（与 feedback.log 同目录）。"""
    from routivus.adaptive.store import data_dir
    return data_dir() / "router.lgb"


if __name__ == "__main__":
    raise SystemExit(main())
