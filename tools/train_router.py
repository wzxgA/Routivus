"""SmartRouter 离线训练 CLI。

标注数据 + feedback.log → TF-IDF + LightGBM，产物默认写
``~/.routivus/adaptive/router.lgb``（数据目录可由 ``ROUTIVUS_ADAPTIVE_DIR`` 覆盖）。

用法：
    python tools/train_router.py labeled.jsonl            # 标注 + feedback 混合训练
    python tools/train_router.py --feedback-only          # 仅 feedback.log（无 TF-IDF 词信号）
    python tools/train_router.py labeled.jsonl --no-semantic
    python tools/train_router.py labeled.jsonl --out m.bin

实现已抽到 ``routivus/adaptive/training.py``（方案 10 §6）：同一套样本构建、列序契约与
训练逻辑同时服务本 CLI、``routivus/adaptive/evolve.py``（本地自动进化）与
``tools/distill_nosem_router.py``（出厂无语义兜底产物的规则蒸馏）。本文件只留 CLI。

样本来源与去噪（细节见 training.build_samples）：
- 标注数据 JSONL，每行 {"text": "...", "tier": 0..3 或档位名}，权重 1.0；
- feedback.log 无原文（只有 text_hash + features 快照），标签由"当时档位 ± 信号方向"
  反推（Δ=±1，到顶/到底丢弃），同 text_hash 先加权投票（平票丢弃），命中硬规则
  （risk / chatty）的记录剔除。

语义列：默认自动探测数据目录里的语义编码器（``router_semantics.onnx``）；
``--semantic-onnx`` 指定其他产物；``--no-semantic`` 关闭（纯 TF-IDF + 数值）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # 脚本独立可跑：保证能 import routivus

from routivus.adaptive.feedback import read_feedback  # noqa: E402
from routivus.adaptive.training import (  # noqa: E402,F401 - 兼容既有导入路径
    ARTIFACT_FORMAT,
    FEATURE_KEYS,
    TIER_NAMES,
    aggregate_by_hash,
    artifact_accuracy,
    build_matrix,
    build_samples,
    derive_label,
    holdout_split,
    load_artifact,
    tier_to_idx,
    train_and_save,
)


def _repo_default_artifact_path() -> Path:
    """默认产物路径：用户数据目录 ~/.routivus/adaptive/router.lgb（与 feedback.log 同目录）。"""
    from routivus.adaptive.store import data_dir
    return data_dir() / "router.lgb"


def _resolve_semantic(args) -> object | None:
    """语义编码器：--no-semantic 关闭；--semantic-onnx 指定；缺省自动探测数据目录产物。"""
    if args.no_semantic:
        return None
    from routivus.router.semantic import SemanticEncoder, load_semantic_encoder

    if args.semantic_onnx:
        semantic = SemanticEncoder(Path(args.semantic_onnx))
    else:
        semantic = load_semantic_encoder()
    if not getattr(semantic, "available", False):
        reason = getattr(semantic, "unavailable_reason", "") or "unknown"
        print(f"提示：语义编码器不可用（{reason}），本次训练不含语义列",
              file=sys.stderr)
    return semantic


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
        help="产物路径，默认 ~/.routivus/adaptive/router.lgb",
    )
    parser.add_argument("--val-size", type=float, default=0.2)
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument(
        "--semantic-onnx", default=None,
        help="bge 语义编码器产物 .onnx 路径（可选）；缺省自动探测数据目录产物",
    )
    parser.add_argument(
        "--no-semantic", action="store_true",
        help="关闭语义列（纯 TF-IDF + 数值特征），产物 sem_dim=0",
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

    semantic = _resolve_semantic(args)
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


if __name__ == "__main__":
    raise SystemExit(main())
