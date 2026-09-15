"""SmartRouter 数据看板（方案 13）的聚合层，供 `GET /api/router/insights` 使用。

把五块数据拼成前端可直接渲染的 JSON：

1. `status`     产物来源 / 语义编码器 / 四档配置 / 文件事实
2. `diagnosis`  一句结论 + 可操作建议（后端生成，前端零硬编码）
3. `runtime`    区间内 `router.updated` 事件的统计（events 表）
4. `calibration` / `rules` / `samples` / `evolution`    adaptive 目录下的文件
5. `limits`     门槛常量（前端只显示、不硬编码）

三条约束：

- **常量与判定只有一份**：门槛、夹紧、上限全部从 `calibrate` / `learned_rules` /
  `evolve` 三个模块 import，结论也在这里生成。前端不再复刻一遍，避免两处漂移。
- **adaptive 文件一律走既有的安全读取**（损坏回退空值），读到空态标 `degraded`，
  绝不因为文件损坏让接口 500。
- **不返回任何原文**：`feedback.log` 只有 `text_hash` 与特征快照、样本库只有量化向量，
  接口连 hash 明细都不给，只给计数。
- **GET 不加载重资产**：产物与编码器优先读"已加载的进程级共享资产"（与 chat 完全
  同源，零漂移）；未加载时只做轻量探测（文件在不在、依赖能不能 import），标
  `verified: false`。这样既不会为了看一眼状态拽起 24MB 的 ONNX 会话，也不会顺带
  重算 calibration / learned_rules（`shared_assets()` 会做这两件事，GET 不该写数据）。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Sequence

from routivus.adaptive.calibrate import (
    MAX_BIAS,
    MAX_THRESHOLD_ADJUST,
    MIN_SAMPLES_PER_TIER,
    load_calibration,
)
from routivus.adaptive.evolve import (
    COOLDOWN_DAYS,
    HOLDOUT_RATIO,
    MIN_NEW,
    MIN_PER_TIER,
    MIN_TOTAL,
    MIN_TOTAL_FIRST,
    _new_since,
    auto_enabled,
    gate_thresholds,
    per_tier_counts,
    read_state,
)
from routivus.adaptive.feedback import SIGNAL_META, SignalType, read_feedback
from routivus.adaptive.learned_rules import (
    MAX_CONFIDENCE,
    MAX_RULES,
    MIN_PRECISION,
    MIN_SUPPORT,
    load_learned_rules,
)
from routivus.adaptive.samples import MAX_RECORDS
from routivus.adaptive.store import (
    calibration_path,
    evolve_log_path,
    learned_rules_path,
    ml_router_nosem_path,
    ml_router_path,
    ml_router_prev_path,
    read_json_safe,
    sem_samples_path,
    semantic_head_path,
    semantic_onnx_path,
)
from routivus.adaptive.training import build_samples
from routivus.router import TIER_NAMES
from routivus.server.routing import loaded_assets
# 同一包内复用日期 / 时区换算：与用量面板口径一致，避免两处各写一套本地日逻辑
from routivus.server.storage import _local_tz, _utc_bounds  # noqa: PLC2701

DEFAULT_DAYS = 30
MIN_DAYS = 7
MAX_DAYS = 90
# 最近 N 轮明细表的条数
MAX_RECENT = 30
# 单次拉取的事件上限：刚好等于上限时标 truncated，不假装是全量
EVENT_LIMIT = 5000
# 信号展示顺序：按权重降序（先看最能说明问题的）
SIGNAL_ORDER = ("clarify", "cmd_retry", "interrupt", "short_high_tier")


def router_insights(
    store: Any,
    *,
    days: int = DEFAULT_DAYS,
    tz: tzinfo | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """聚合一次看板数据。`store` 只需实现 `list_events_by_type`。"""
    local_tz = tz or _local_tz()
    current = now.astimezone(local_tz) if now is not None else datetime.now(local_tz)
    span = max(MIN_DAYS, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    today: date = current.date()
    since_iso, until_iso = _utc_bounds(today - timedelta(days=span - 1), today + timedelta(days=1), local_tz)

    assets = loaded_assets()
    status = _status_block(assets)
    runtime = _runtime_block(store, since_iso, until_iso)
    calibration = _calibration_block(assets)
    rules = _rules_block(assets)
    samples = _samples_block(current.timestamp())
    evolution = _evolution_block(samples, current.timestamp())
    limits = _limits()

    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": str(local_tz),
        "days": span,
        "limits": limits,
        "status": status,
        "diagnosis": _diagnose(status, runtime, samples, evolution),
        "runtime": runtime,
        "calibration": calibration,
        "rules": rules,
        "samples": samples,
        "evolution": evolution,
    }


# ---------------------------------------------------------------------------
# 状态：产物 / 编码器（优先读已加载的共享资产，未加载时轻量探测）
# ---------------------------------------------------------------------------


def _status_block(assets: Any) -> dict[str, Any]:
    if assets is not None:
        ml = getattr(assets, "ml", None)
        return {
            "verified": True,
            "load_seconds": round(float(getattr(assets, "load_seconds", 0.0) or 0.0), 3),
            "artifact": _loaded_artifact(ml),
            "semantic": _loaded_semantic(getattr(ml, "semantic", None)),
            "prev_artifact": _file_facts(ml_router_prev_path()),
        }
    artifact, semantic = _probe()
    return {
        "verified": False,
        "load_seconds": 0.0,
        "artifact": artifact,
        "semantic": semantic,
        "prev_artifact": _file_facts(ml_router_prev_path()),
    }


def _loaded_artifact(ml: Any) -> dict[str, Any]:
    path = getattr(ml, "artifact_path", None)
    return {
        "source": str(getattr(ml, "source", "") or "unavailable"),
        "reason_code": str(getattr(ml, "unavailable_reason", "") or ""),
        "n_samples": getattr(ml, "n_samples", None),
        "val_accuracy": getattr(ml, "val_accuracy", None),
        "trained_at": _epoch_iso(getattr(ml, "trained_at", None)),
        "sem_dim": int(getattr(ml, "sem_dim", 0) or 0),
        "head_dim": int(getattr(ml, "head_dim", 0) or 0),
        "eval_error": "",
        **_file_facts(path),
    }


def _loaded_semantic(encoder: Any) -> dict[str, Any]:
    path = semantic_onnx_path()
    available = bool(getattr(encoder, "available", False))
    return {
        "available": available,
        "reason_code": str(getattr(encoder, "unavailable_reason", "") or ""),
        "dim": int(getattr(encoder, "dim", 0) or 0) if available else 0,
        "calls": int(getattr(encoder, "calls", 0) or 0),
        "avg_ms": round(float(getattr(encoder, "avg_ms", 0.0) or 0.0), 2),
        **_file_facts(path),
    }


def _probe() -> tuple[dict[str, Any], dict[str, Any]]:
    """未加载重资产时的轻量预判：只看文件与依赖，不建 ONNX 会话。

    产物来源按 `ml_router._load` 的同一条链条判断（语义版要求编码器可用），但
    **不加载模型**，所以标 `verified: false`；内嵌字段（样本数、验证准确率）改从
    产物文件直接读一次（36KB / 1.86MB，比 ONNX 便宜得多）。
    """
    sem_ok, sem_reason = _probe_semantic()
    onnx = _file_facts(semantic_onnx_path())
    semantic = {
        "available": sem_ok,
        "reason_code": sem_reason,
        "dim": 512 if sem_ok else 0,
        "calls": 0,
        "avg_ms": 0.0,
        **onnx,
    }

    sem_artifact = ml_router_path()
    nosem_artifact = ml_router_nosem_path()
    if sem_ok and sem_artifact.exists():
        source, path, reason_code = "semantic", sem_artifact, ""
    elif nosem_artifact.exists():
        source, path = "nosem", nosem_artifact
        # 语义版没被采用的原因要单独说清：编码器不可用，或语义产物缺失
        reason_code = "" if sem_ok else ("no_semantic" if sem_artifact.exists() else "no_artifact")
    else:
        # 与 ml_router 一致：取链条最后一级的原因（它最接近"为什么最终没有可用产物"）
        source, path, reason_code = "unavailable", None, "no_artifact"

    artifact = {
        "source": source,
        "reason_code": reason_code,
        "n_samples": None,
        "val_accuracy": None,
        "trained_at": "",
        "sem_dim": 0,
        "head_dim": 0,
        "eval_error": "",
        **_artifact_meta(path),
        **_file_facts(path),
    }
    return artifact, semantic


def _probe_semantic() -> tuple[bool, str]:
    """只判断"依赖能不能 import + 两个文件在不在"，不创建 InferenceSession。"""
    onnx = semantic_onnx_path()
    if not onnx.exists():
        return False, "artifact_missing"
    if not onnx.with_suffix(".json").exists():
        return False, "tokenizer_missing"
    try:
        import onnxruntime  # noqa: F401, PLC0415
        from tokenizers import Tokenizer  # noqa: F401, PLC0415
    except ImportError:
        return False, "runtime_missing"
    return True, ""


def _artifact_meta(path: Path | None) -> dict[str, Any]:
    """读产物内嵌的展示字段；损坏 / 缺依赖时给 None 并记 eval_error（不影响其它块）。"""
    empty: dict[str, Any] = {"n_samples": None, "val_accuracy": None, "trained_at": "",
                             "sem_dim": 0, "head_dim": 0}
    if path is None or not path.exists():
        return empty
    try:
        import joblib  # noqa: PLC0415

        payload = joblib.load(path)
    except Exception:  # noqa: BLE001 - 缺 lightgbm / 产物损坏都只影响这一块
        return {**empty, "eval_error": "load_failed"}
    if not isinstance(payload, dict):
        return {**empty, "eval_error": "bad_artifact"}
    return {
        "n_samples": payload.get("n_samples"),
        "val_accuracy": payload.get("val_accuracy"),
        "trained_at": _epoch_iso(payload.get("trained_at")),
        "sem_dim": int(payload.get("sem_dim", 0) or 0),
        "head_dim": int(payload.get("head_dim", 0) or 0),
        "eval_error": "",
    }


def _file_facts(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "present": False, "bytes": 0, "mtime": ""}
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "present": False, "bytes": 0, "mtime": ""}
    return {"path": str(path), "present": True, "bytes": stat.st_size, "mtime": _epoch_iso(stat.st_mtime)}


def _epoch_iso(value: Any) -> str:
    try:
        moment = datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return ""
    return moment.astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 运行态：router.updated 事件统计
# ---------------------------------------------------------------------------


def _runtime_block(store: Any, since: str, until: str) -> dict[str, Any]:
    try:
        records = store.list_events_by_type("router.updated", since=since, until=until, limit=EVENT_LIMIT)
    except Exception:  # noqa: BLE001 - 统计失败只让这一块空，不影响其它块
        records = []

    by_tier: dict[str, int] = {name: 0 for name in TIER_NAMES}
    by_tier["unknown"] = 0
    buckets = {"<0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, ">=0.9": 0}
    load_ms: list[int] = []
    route_ms: list[int] = []
    errors: list[str] = []
    hard_rule = 0
    switched = 0
    recent: list[dict[str, Any]] = []

    for record in records:
        data = dict(getattr(record, "data", None) or {})
        tier = str(data.get("tier") or "")
        by_tier[tier if tier in TIER_NAMES else "unknown"] += 1
        error = str(data.get("error") or "")
        entry = {
            "ts": str(getattr(record, "occurred_at", "")),
            "tier": tier,
            "confidence": _as_float(data.get("confidence")),
            "score": _as_float(data.get("score")),
            "hard_rule": bool(data.get("hard_rule")),
            "notes": [str(item) for item in (data.get("notes") or [])],
            "elapsed_ms": _as_int(data.get("elapsed_ms")),
            "switched": bool(data.get("switched")),
            "error": error,
        }
        recent.append(entry)
        if error:
            errors.append(error)
            continue  # 失败轮没有档位与置信度，只计入 total 与失败分组
        if entry["hard_rule"]:
            hard_rule += 1
        buckets[_bucket_of(entry["confidence"])] += 1
        if entry["switched"]:
            switched += 1
        if entry["elapsed_ms"] is not None:
            route_ms.append(entry["elapsed_ms"])
        load_value = _as_int(data.get("load_ms"))
        if load_value:
            load_ms.append(load_value)

    routed = sum(count for tier, count in by_tier.items() if tier != "unknown")
    error_groups = [
        {"text": text, "count": count} for text, count in Counter(errors).most_common(5)
    ]
    return {
        "total": len(records),
        "routed": routed,
        "errors": len(errors),
        "events_since": since,
        "truncated": len(records) >= EVENT_LIMIT,
        "by_tier": by_tier,
        "hard_rule": hard_rule,
        "hard_rule_ratio": round(hard_rule / routed, 4) if routed else 0.0,
        "confidence_buckets": buckets,
        "switched": switched,
        "latency_ms": {
            "load_p50": _percentile(load_ms, 0.5),
            "load_p95": _percentile(load_ms, 0.95),
            "route_p50": _percentile(route_ms, 0.5),
            "route_p95": _percentile(route_ms, 0.95),
            "route_max": max(route_ms) if route_ms else None,
        },
        "error_groups": error_groups,
        "recent": recent[-MAX_RECENT:],
    }


def _bucket_of(confidence: float | None) -> str:
    value = confidence or 0.0
    if value < 0.5:
        return "<0.5"
    if value < 0.7:
        return "0.5-0.7"
    if value < 0.9:
        return "0.7-0.9"
    return ">=0.9"


def _percentile(values: Sequence[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(int(item) for item in values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


# ---------------------------------------------------------------------------
# 学习数据：校准 / 规则 / 样本
# ---------------------------------------------------------------------------


def _calibration_block(assets: Any) -> dict[str, Any]:
    cal = getattr(assets, "calibration", None) if assets is not None else None
    if cal is None:
        cal = load_calibration()
    data = cal.to_dict() if hasattr(cal, "to_dict") else {}
    path = calibration_path()
    return {
        "degraded": _looks_corrupt(path),
        "bias": data.get("bias") or {},
        "samples": data.get("samples") or {},
        "threshold_adjust": _as_float(data.get("threshold_adjust")) or 0.0,
        "total": _as_float(data.get("total")) or 0.0,
    }


def _rules_block(assets: Any) -> dict[str, Any]:
    rules = getattr(assets, "learned", None) if assets is not None else None
    if rules is None:
        rules = load_learned_rules()
    items = [
        {
            "feature": rule.feature,
            "op": rule.op,
            "value": rule.value,
            "action": int(rule.action),
            "confidence": float(rule.confidence),
            "support": float(rule.support),
        }
        for rule in getattr(rules, "rules", ()) or ()
    ]
    return {"degraded": _looks_corrupt(learned_rules_path()), "items": items}


def _samples_block(now_ts: float) -> dict[str, Any]:
    """样本概况。

    训练样本（`build_samples`）里 tier 是**从信号方向反推**的，与 feedback 原始行一一
    对应去重后的结果。这里**不传语义向量**（`build_samples(..., {})`）：语义列只影响
    特征列，不影响档位分布，省掉为看一眼看板而反量化 5 万条向量的开销。
    """
    records = read_feedback()
    try:
        samples, build_stats = build_samples(None, records, {})
    except Exception:  # noqa: BLE001 - 样本构建失败只让这一块降级
        samples, build_stats = [], {}

    by_signal: dict[str, dict[str, int]] = {}
    for name in SIGNAL_ORDER:
        by_signal[name] = {"up": 0, "down": 0}
    for record in records:
        name = str(record.get("source") or "")
        if name not in by_signal:
            by_signal[name] = {"up": 0, "down": 0}
        direction = str(record.get("signal") or "")
        if direction == "upgrade":
            by_signal[name]["up"] += 1
        elif direction == "downgrade":
            by_signal[name]["down"] += 1

    counts = per_tier_counts(samples)
    by_tier = {name: 0 for name in TIER_NAMES}
    for idx, count in counts.items():
        if 0 <= int(idx) < len(TIER_NAMES):
            by_tier[TIER_NAMES[int(idx)]] = int(count)

    timestamps = [float(record.get("ts") or 0.0) for record in records]
    return {
        "degraded": bool(records) and not build_stats,
        "feedback_lines": len(records),
        "available_samples": len(samples),
        "weighted_total": round(sum(float(record.get("weight") or 0.0) for record in records), 3),
        "unique_texts": len({str(record.get("text_hash")) for record in records if record.get("text_hash")}),
        "by_tier": by_tier,
        "by_signal": by_signal,
        "build_stats": build_stats,
        "new_7d": _count_since(timestamps, now_ts - 7 * 86400.0),
        "new_30d": _count_since(timestamps, now_ts - 30 * 86400.0),
        "sem_samples": _sem_sample_stats(),
        "semantic_head": _head_stats(),
    }


def _sem_sample_stats() -> dict[str, Any]:
    """只数条数与时间范围。

    刻意不用 `read_sem_samples()`：它会把每行 base64 解码成 512 维浮点（5 万条 ≈ 200MB
    的 Python 对象），而在看板上只要"多少条、覆盖到什么时间"。
    """
    path = sem_samples_path()
    stats: dict[str, Any] = {"count": 0, "bytes": 0, "max": MAX_RECORDS, "newest_ts": 0.0, "oldest_ts": 0.0}
    if not path.exists():
        return stats
    try:
        stats["bytes"] = path.stat().st_size
        newest = 0.0
        oldest = 0.0
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats["count"] += 1
                try:
                    ts = float(json.loads(line).get("ts") or 0.0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if ts and (not oldest or ts < oldest):
                    oldest = ts
                if ts > newest:
                    newest = ts
        stats["newest_ts"] = newest
        stats["oldest_ts"] = oldest
    except OSError:
        pass
    return stats


def _head_stats() -> dict[str, Any]:
    raw = read_json_safe(semantic_head_path())
    centroids = raw.get("centroids") if isinstance(raw, dict) else None
    return {
        "present": isinstance(centroids, list) and bool(centroids),
        "tiers": len(centroids) if isinstance(centroids, list) else 0,
        "dim": int(raw.get("dim", 0) or 0) if isinstance(raw, dict) else 0,
        "train_acc": _as_float(raw.get("train_acc")) if isinstance(raw, dict) else None,
    }


# ---------------------------------------------------------------------------
# 进化：门槛进度 + 决策历史
# ---------------------------------------------------------------------------


def _evolution_block(samples: dict[str, Any], now_ts: float) -> dict[str, Any]:
    state = read_state()
    limits = gate_thresholds(state)
    gated_samples = _samples_for_gates()
    counts = per_tier_counts(gated_samples)
    total = sum(counts.values())
    new_since = _new_since(gated_samples, state.last_success_at)

    remaining = 0.0
    if state.last_attempt_at:
        elapsed_days = (now_ts - state.last_attempt_at) / 86400.0
        remaining = max(0.0, round(limits["cooldown_days"] - elapsed_days, 2))

    gates = [
        {
            "key": "tiers",
            "label": "至少两个档位有标签",
            "satisfied": len(counts) >= 2,
            "detail": f"{len(counts)} 个档位" if counts else "0 个档位",
        },
        {
            "key": "per_tier",
            "label": f"每个出现的档位 ≥{int(limits['min_per_tier'])} 条",
            "satisfied": bool(counts) and all(v >= limits["min_per_tier"] for v in counts.values()),
            "detail": "、".join(
                f"{TIER_NAMES[int(idx)]} {count}/{int(limits['min_per_tier'])}"
                for idx, count in sorted(counts.items())
            ) or "还没有样本",
        },
        {
            "key": "total",
            "label": f"总量 ≥{int(limits['min_total'])}（首次 {MIN_TOTAL_FIRST}）",
            "satisfied": total >= limits["min_total"],
            "detail": f"{total}/{int(limits['min_total'])}",
        },
        {
            "key": "cooldown",
            "label": f"距上次尝试 ≥{limits['cooldown_days']:.0f} 天",
            "satisfied": remaining <= 0,
            "detail": "从未尝试" if not state.last_attempt_at else f"还剩 {remaining:.1f} 天",
        },
        {
            "key": "new_since_success",
            "label": f"自上次成功新增 ≥{int(limits['min_new'])} 条",
            "satisfied": new_since >= limits["min_new"],
            "detail": f"{new_since}/{int(limits['min_new'])}",
        },
    ]

    return {
        "state": state.to_dict(),
        "auto_evolve": auto_enabled(),
        "thresholds": limits,
        "cooldown_remaining_days": remaining,
        "gates": gates,
        "counts_by_tier": {TIER_NAMES[int(idx)]: count for idx, count in sorted(counts.items())
                           if 0 <= int(idx) < len(TIER_NAMES)},
        "total_samples": total,
        "new_since_success": new_since,
        "history": _read_evolve_log(),
    }


_SAMPLES_CACHE: tuple[float, list[dict[str, Any]]] | None = None


def _samples_for_gates() -> list[dict[str, Any]]:
    """门槛用的样本集（进程内缓存 5 秒）。

    `_samples_block` 与 `_evolution_block` 都要它，而构建一次要解析整个 feedback.log；
    同一次请求里复用即可，跨请求留 5 秒避免连续刷新重复解析。
    """
    global _SAMPLES_CACHE
    now = datetime.now().timestamp()
    if _SAMPLES_CACHE is not None and now - _SAMPLES_CACHE[0] < 5.0:
        return _SAMPLES_CACHE[1]
    try:
        samples, _ = build_samples(None, read_feedback(), {})
    except Exception:  # noqa: BLE001
        samples = []
    _SAMPLES_CACHE = (now, samples)
    return samples


def _read_evolve_log(limit: int = 50) -> list[dict[str, Any]]:
    """evolve.log 尾部若干行（JSONL）；坏行跳过，绝不让看板报错。"""
    path = evolve_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            items.append(record)
    return items


# ---------------------------------------------------------------------------
# 结论（后端生成，前端只渲染）
# ---------------------------------------------------------------------------

# 四条隐式信号的可读说法：空态提示要用
_SIGNAL_HINT = "只有追问 / 否定、重试类输入、短问句落到高档、回答中途中断 这几类行为会产生学习信号"


def _diagnose(
    status: dict[str, Any],
    runtime: dict[str, Any],
    samples: dict[str, Any],
    evolution: dict[str, Any],
) -> dict[str, Any]:
    artifact = status.get("artifact") or {}
    semantic = status.get("semantic") or {}
    source = str(artifact.get("source") or "")
    reason = str(artifact.get("reason_code") or "")
    sem_reason = str(semantic.get("reason_code") or "")

    total = int(runtime.get("total") or 0)
    errors = int(runtime.get("errors") or 0)
    if total and errors and errors / total >= 0.05:
        return _verdict("error", "route_errors",
                        f"区间内 {errors}/{total} 次路由失败，失败的轮次沿用当前模型。",
                        ["展开「最近路由」看错误文案；常见原因是 provider Key 失效或路由超时",
                         "超时可调大 ROUTIVUS_ROUTER_TIMEOUT，或在配置页关掉智能路由"])
    if source == "unavailable":
        return _verdict("error", "ml_unavailable",
                        f"ML 精判不可用（{reason}），本轮档位完全由规则与校准决定。",
                        [_fix_for(reason)])
    if source == "nosem":
        return _verdict("warn", "ml_nosem",
                        f"ML 精判正在用无语义兜底产物（原因：{reason or '语义版不可用'}）。",
                        [f"语义编码器：{_fix_for(sem_reason)}"] if sem_reason else
                        ["语义版产物缺失，重启后端会自动从随包产物落位"])
    if sem_reason:
        return _verdict("warn", "semantic_unavailable",
                        f"语义通道不可用（{sem_reason}），ML 精判退化为无法使用语义列。",
                        [_fix_for(sem_reason)])
    if int(samples.get("feedback_lines") or 0) == 0:
        return _verdict("warn", "no_samples",
                        "还没有任何隐式信号，校准与本地进化都不会推进。",
                        [_SIGNAL_HINT,
                         "本站的停止按钮不产生信号：interrupt 只在旧 TUI 的内联循环里采集"])
    state = evolution.get("state") or {}
    pending = [gate for gate in evolution.get("gates") or [] if not gate.get("satisfied")]
    if pending:
        return _verdict("warn", "gate_not_met",
                        f"还没达到本地进化门槛，当前 {evolution.get('total_samples')} 条可用样本。",
                        [f"{gate['label']}：{gate['detail']}" for gate in pending[:3]])
    if str(state.get("last_decision") or "") == "rejected":
        return _verdict("warn", "rejected_worse",
                        "上次训练出的候选产物没有通过不劣门，已被丢弃并保留当前产物。",
                        ["不劣门只在本地产物之间比较：继续积累样本，下次更可能通过"])
    remaining = float(evolution.get("cooldown_remaining_days") or 0.0)
    if remaining > 0:
        return _verdict("ok", "cooldown", f"门槛已满足，冷却中：还有 {remaining:.1f} 天可以再试。", [])
    return _verdict("ok", "healthy",
                    "路由与学习链路都正常，样本够了会自动进化。", [])


def _verdict(level: str, code: str, text: str, hints: list[str]) -> dict[str, Any]:
    return {"level": level, "code": code, "text": text, "hints": hints}


def _fix_for(reason: str) -> str:
    """原因码 → 怎么办（与既有 status / 前端提示同一套词汇）。"""
    return {
        "runtime_missing": "缺依赖：onnxruntime / tokenizers 装不上或装坏了，重新同步依赖后重启后端",
        "no_semantic": "语义版产物需要编码器：先修好编码器，或等它回落到无语义兜底产物",
        "artifact_missing": "产物文件不在数据目录：重启后端会从随包产物自动落位",
        "tokenizer_missing": "缺与 onnx 同名的 tokenizer 文件：重启后端会从随包产物自动落位",
        "dim_mismatch": "产物输出维度不是 512：重新导出语义产物",
        "load_failed": "产物加载失败（可能损坏）：删掉数据目录下的产物后重启，会重新落位",
        "no_artifact": "两个产物都不在数据目录：重启后端会从随包产物自动落位",
        "version_mismatch": "产物结构版本与本版本不匹配：删掉后重启会重新落位",
    }.get(reason, "查看数据目录下的产物与依赖状态")


# ---------------------------------------------------------------------------
# 常量块
# ---------------------------------------------------------------------------


def _limits() -> dict[str, Any]:
    return {
        "calibration_min_samples": MIN_SAMPLES_PER_TIER,
        "max_bias": MAX_BIAS,
        "max_threshold_adjust": MAX_THRESHOLD_ADJUST,
        "rule_min_support": MIN_SUPPORT,
        "rule_min_precision": MIN_PRECISION,
        "rule_max_confidence": MAX_CONFIDENCE,
        "rules_max": MAX_RULES,
        "evolve_min_per_tier": MIN_PER_TIER,
        "evolve_min_total": MIN_TOTAL,
        "evolve_min_total_first": MIN_TOTAL_FIRST,
        "evolve_min_new": MIN_NEW,
        "evolve_cooldown_days": COOLDOWN_DAYS,
        "holdout_ratio": HOLDOUT_RATIO,
        "sem_samples_max": MAX_RECORDS,
        "signals": {
            name: {"upgrade": bool(SIGNAL_META[SignalType(name)]["upgrade"]),
                   "weight": float(SIGNAL_META[SignalType(name)]["weight"])}
            for name in SIGNAL_ORDER
        },
    }


def _looks_corrupt(path: Path) -> bool:
    """文件在、但读不出合法 JSON → 判定为损坏（空态由空 dict 兜底）。"""
    return path.exists() and not isinstance(read_json_safe(path), dict)


def _count_since(timestamps: Sequence[float], since_ts: float) -> int:
    return sum(1 for ts in timestamps if ts >= since_ts)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
