"""本地进化：用本机反馈重训 ML 产物（方案 10 §4.2/§4.3）。

一句话：**出厂产物是起点，本地数据决定终点**。每台机器上的
``feedback.log``（隐式信号）+ ``sem_samples.jsonl``（语义向量）都不同，
所以进化后的 ``router.lgb`` 天然是"自己那一份"。

四道硬门（缺一条就不替换，宁可不动也不学坏）:

1. **样本门槛**：至少 2 个档位有标签，且每个出现的档位 ≥ ``MIN_PER_TIER``(20)；
   总样本 ≥ ``MIN_TOTAL``(60)，首次进化 ≥ ``MIN_TOTAL_FIRST``(120)；
2. **增量门槛**：距上次成功新增可用样本 ≥ ``MIN_NEW``(20)；
3. **冷却**：距上次**尝试** ≥ ``COOLDOWN_DAYS``(7) 天（失败也算一次尝试，避免反复重试）；
4. **不劣门**：新产物在同一 holdout（按时间切分）上加权准确率**不得低于**当前在用
   产物；验证不过就丢弃并记日志（``evolve.log``），绝不替换。

训练与评估都在**子进程**里跑（``python -m routivus.adaptive.evolve``）：
``server/routing.py:119-124`` 记录过一次真实事故——重型 import 放后台线程会与事件
循环首个 AnyIO worker 抢导入锁而死锁。子进程还顺带解决了 GIL、内存峰值与崩溃隔离。

环境变量（都可选）：

- ``ROUTIVUS_ADAPTIVE_EVOLVE``：``off`` 关闭自动进化（手动 ``/smartRouter evolve`` 仍可用）
- ``ROUTIVUS_ADAPTIVE_COOLDOWN_DAYS`` / ``_MIN_NEW`` / ``_CHECK_ROUNDS``
- ``ROUTIVUS_SERVER_RUNTIME``：只有真实服务进程（``__main__`` 设置）才允许自动触发，
  测试与库调用永远不会偷偷拉起子进程
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .store import (
    _atomic_copy,
    append_jsonl,
    atomic_write_json,
    evolve_log_path,
    evolve_state_path,
    ml_router_path,
    ml_router_prev_path,
    read_json_safe,
    semantic_head_path,
)

logger = logging.getLogger("routivus.adaptive.evolve")

MIN_PER_TIER = 20
MIN_TOTAL = 60
MIN_TOTAL_FIRST = 120
MIN_NEW = 20
COOLDOWN_DAYS = 7.0
HOLDOUT_RATIO = 0.2
CHECK_ROUNDS = 50
LOCK_STALE_SECONDS = 3600.0  # 锁文件超过 1 小时视为残留（崩溃/断电）


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def auto_enabled() -> bool:
    """自动进化开关（默认开）。手动 ``/smartRouter evolve`` 不受它影响。"""
    return os.environ.get("ROUTIVUS_ADAPTIVE_EVOLVE", "on").strip().lower() not in {
        "off", "0", "false", "no",
    }


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

@dataclass
class EvolveState:
    """进化状态。``last_attempt_at`` 与 ``last_success_at`` 必须分开记（方案 10 §4.3）。"""

    last_attempt_at: float = 0.0
    last_success_at: float = 0.0
    samples_since_success: int = 0
    last_decision: str = ""
    last_reason: str = ""
    last_holdout_acc: float | None = None
    last_baseline_acc: float | None = None
    evolve_count: int = 0

    @classmethod
    def from_dict(cls, data: Any) -> "EvolveState":
        if not isinstance(data, dict):
            return cls()
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_state() -> EvolveState:
    return EvolveState.from_dict(read_json_safe(evolve_state_path(), {}))


def write_state(state: EvolveState) -> None:
    try:
        atomic_write_json(evolve_state_path(), state.to_dict())
    except Exception:  # noqa: BLE001 - 状态写不进去不该影响路由
        logger.debug("evolve: 状态写盘失败", exc_info=True)


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------

def collect_samples() -> tuple[list[dict[str, Any]], dict[str, int]]:
    """feedback.log + 本地语义样本 → 训练样本（无原文，方案 10 §4.1）。"""
    from .feedback import read_feedback
    from .samples import read_sem_samples
    from .training import build_samples

    sem_by_hash = read_sem_samples()
    samples, stats = build_samples(None, read_feedback(), sem_by_hash)
    return samples, stats


def per_tier_counts(samples: list[dict[str, Any]]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for s in samples:
        idx = int(s["tier"])
        counts[idx] = counts.get(idx, 0) + 1
    return counts


def _new_since(samples: list[dict[str, Any]], since_ts: float) -> int:
    """自 ``since_ts`` 以来的可用样本数（ts 缺失的按旧样本处理，不推动进化）。"""
    if not since_ts:
        return len(samples)
    return sum(1 for s in samples if float(s.get("ts", 0.0) or 0.0) > since_ts)


def gate_thresholds(state: EvolveState) -> dict[str, float]:
    """当前生效的门槛值（含 env 覆盖，以及"首次 / 非首次"的量级差别）。

    `gate()` 与数据看板（`server/insights.py`）共用这一份，避免两处常量漂移；
    冷却给"天"而不是秒，因为看板要显示"还有几天可以再试"。
    """
    return {
        "min_per_tier": float(MIN_PER_TIER),
        "min_total": float(MIN_TOTAL_FIRST if state.evolve_count == 0 else MIN_TOTAL),
        "cooldown_days": _float_env("ROUTIVUS_ADAPTIVE_COOLDOWN_DAYS", COOLDOWN_DAYS),
        "min_new": float(_int_env("ROUTIVUS_ADAPTIVE_MIN_NEW", MIN_NEW)),
    }


def gate(samples: list[dict[str, Any]], state: EvolveState,
         *, now: float | None = None) -> str:
    """门槛检查。返回空串表示可以进化，否则返回跳过原因码。"""
    now = now if now is not None else time.time()
    counts = per_tier_counts(samples)
    total = len(samples)
    limits = gate_thresholds(state)
    if len(counts) < 2:
        return "not_enough_tiers"
    if any(v < limits["min_per_tier"] for v in counts.values()):
        return "not_enough_per_tier"
    if total < limits["min_total"]:
        return "not_enough_total"
    if state.last_attempt_at and (now - state.last_attempt_at) < limits["cooldown_days"] * 86400.0:
        return "cooldown"
    if _new_since(samples, state.last_success_at) < limits["min_new"]:
        return "not_enough_new"
    return ""


# ---------------------------------------------------------------------------
# 锁（子进程互斥）
# ---------------------------------------------------------------------------

class _Lock:
    """``evolve.lock`` 文件锁：进程崩溃残留的锁在 1 小时后可被接管。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> bool:
        from .store import ensure_dir

        try:
            ensure_dir()
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                    self.path.unlink(missing_ok=True)
                    return self.__enter__()
            except OSError:
                pass
            return False
        except OSError:
            return False

    def __exit__(self, *exc: object) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _log(record: dict[str, Any]) -> None:
    try:
        append_jsonl(evolve_log_path(), record)
    except Exception:  # noqa: BLE001
        logger.debug("evolve: 日志写入失败", exc_info=True)


def run_evolve(trigger: str = "manual", *, force: bool = False,
               now: float | None = None) -> dict[str, Any]:
    """跑一次进化：门槛 → 训练 → holdout 对比 → 不劣才替换。绝不抛错。

    ``force=True``（``/smartRouter evolve --force``）跳过门槛与不劣门之外的前置检查，
    但**仍然守"不劣于当前产物"**——那是最后一道防线。
    """
    from .store import evolve_lock_path

    now = now if now is not None else time.time()
    base: dict[str, Any] = {"trigger": trigger, "ts": now, "decision": "skipped"}

    with _Lock(evolve_lock_path()) as acquired:
        if not acquired:
            base.update(reason="locked")
            _log(base)
            return base
        try:
            return _run_locked(trigger, force=force, now=now, base=base)
        except Exception as exc:  # noqa: BLE001 - 任何异常都只记日志
            logger.warning("evolve 失败：%s", exc, exc_info=True)
            base.update(decision="error", reason=type(exc).__name__)
            _log(base)
            state = read_state()
            state.last_attempt_at = now
            state.last_decision = "error"
            state.last_reason = type(exc).__name__
            write_state(state)
            return base


def _run_locked(trigger: str, *, force: bool, now: float,
                base: dict[str, Any]) -> dict[str, Any]:
    from .training import artifact_accuracy, holdout_split, load_artifact, train_and_save

    state = read_state()
    samples, stats = collect_samples()
    counts = per_tier_counts(samples)
    base.update(samples=len(samples), per_tier={str(k): v for k, v in sorted(counts.items())},
                stats=stats)

    if not force:
        reason = gate(samples, state, now=now)
        if reason:
            # 门槛不过：不更新 last_attempt_at（避免"样本不够"把冷却期烧掉）
            base.update(reason=reason)
            _log(base)
            return base
    elif len(counts) < 2:
        base.update(reason="not_enough_tiers")
        _log(base)
        return base

    # holdout 按**时间**切分（方案 10 §4.2）：最近 20% 只用于评估
    train_rows, holdout_rows = holdout_split(samples, HOLDOUT_RATIO)
    if not holdout_rows:
        # 样本太少时退化为全量训练 + 相同集合评估（仍走不劣门）
        train_rows = list(range(len(samples)))
        holdout_rows = list(range(len(samples)))
    train_samples = [samples[i] for i in train_rows]
    holdout = [samples[i] for i in holdout_rows]

    # L3 本地语义头（方案 10 §4.7）：冻结向量上的每档质心，作为额外 4 列喂给模型。
    # 样本不足时返回 None（不硬凑），此时模型只用 TF-IDF + 数值 + 语义列。
    from .semantic_head import save_head, train_head

    head = train_head(train_samples, holdout_ratio=HOLDOUT_RATIO)
    save_head(head, semantic_head_path())
    if head is not None:
        base["head_train_acc"] = head.train_acc
        base["head_holdout_acc"] = head.holdout_acc

    tmp_path = ml_router_path().with_name(ml_router_path().name + ".evolving")
    try:
        report = train_and_save(
            train_samples, tmp_path,
            semantic=None,  # 本地样本没有原文：语义列来自样本自带的向量
            head=head,
        )
    except SystemExit as exc:  # train_and_save 的样本不足/缺依赖提示
        tmp_path.unlink(missing_ok=True)
        base.update(reason=f"train_failed:{exc}")
        _log(base)
        return base

    candidate = load_artifact(tmp_path)
    holdout_acc = artifact_accuracy(candidate, holdout)
    baseline = load_artifact(ml_router_path())
    baseline_acc = artifact_accuracy(baseline, holdout)
    base.update(holdout_acc=holdout_acc, baseline_acc=baseline_acc,
                n_train=len(train_samples), n_holdout=len(holdout),
                artifact_bytes=report.get("artifact_bytes"))

    if holdout_acc is None:
        tmp_path.unlink(missing_ok=True)
        base.update(reason="unevaluable")
        _log(base)
        return base
    # 不劣门：只有基线可评估时才比较；基线不可读（缺依赖/损坏）时接受候选
    if baseline_acc is not None and holdout_acc < baseline_acc:
        tmp_path.unlink(missing_ok=True)
        base.update(reason="worse_than_current")
        _log(base)
        state.last_attempt_at = now
        state.last_decision = "rejected"
        state.last_reason = "worse_than_current"
        state.last_holdout_acc = holdout_acc
        state.last_baseline_acc = baseline_acc
        write_state(state)
        return base

    # 替换：先备份当前产物（回滚用），再原子落位
    current = ml_router_path()
    if current.exists():
        try:
            _atomic_copy(current, ml_router_prev_path())
        except Exception:  # noqa: BLE001 - 备份失败就放弃这次替换（宁可不动）
            tmp_path.unlink(missing_ok=True)
            base.update(reason="backup_failed")
            _log(base)
            return base
    os.replace(tmp_path, current)

    state.last_attempt_at = now
    state.last_success_at = now
    state.samples_since_success = 0
    state.evolve_count += 1
    state.last_decision = "replaced"
    state.last_reason = ""
    state.last_holdout_acc = holdout_acc
    state.last_baseline_acc = baseline_acc
    write_state(state)
    base.update(decision="replaced", reason="")
    _log(base)
    # 让当前进程（若是服务端）尽快用上新产物
    try:
        from routivus.server.routing import reset_shared_assets
        reset_shared_assets()
    except Exception:  # noqa: BLE001 - 子进程里通常没有服务端，忽略
        pass
    logger.info("evolve: 已替换本地产物（holdout %.3f，基线 %s）",
                holdout_acc, f"{baseline_acc:.3f}" if baseline_acc is not None else "N/A")
    return base


# ---------------------------------------------------------------------------
# 自动触发（方案 10 §4.3）
# ---------------------------------------------------------------------------

_turns_since_check = 0


def spawn(trigger: str, *, force: bool = False, require_runtime: bool = True) -> bool:
    """拉起演化子进程（不等待）。返回是否真的拉起。

    测试安全：``require_runtime=True``（自动触发）时只允许
    ``ROUTIVUS_SERVER_RUNTIME=1``（由 ``routivus/server/__main__.py`` 设置）的进程，
    ``TestClient`` / 库调用永远不会偷偷训练；用户显式敲命令（手动触发）传
    ``require_runtime=False``。
    """
    if require_runtime and os.environ.get("ROUTIVUS_SERVER_RUNTIME") != "1":
        return False
    if not auto_enabled() and require_runtime:
        return False
    try:
        env = {**os.environ, "ROUTIVUS_SERVER_RUNTIME": "0"}
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "DETACHED_PROCESS", 0)
        argv = [sys.executable, "-m", "routivus.adaptive.evolve",
                "--trigger", trigger]
        if force:
            argv.append("--force")
        subprocess.Popen(
            argv,
            env=env, cwd=os.getcwd(), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        logger.info("evolve: 已拉起后台演化进程（trigger=%s force=%s）", trigger, force)
        return True
    except Exception:  # noqa: BLE001 - 拉不起来不影响任何功能
        logger.warning("evolve: 拉起子进程失败", exc_info=True)
        return False


def maybe_spawn(trigger: str) -> bool:
    """轻量判断（只读计数与时间）后再拉起子进程：不在主进程做任何训练。"""
    if os.environ.get("ROUTIVUS_SERVER_RUNTIME") != "1" or not auto_enabled():
        return False
    try:
        state = read_state()
        samples, _ = collect_samples()
        if gate(samples, state):
            return False
    except Exception:  # noqa: BLE001
        return False
    return spawn(trigger)


def note_turn() -> None:
    """每轮对话调用一次；累计到 ``CHECK_ROUNDS`` 轮才做一次门槛检查。"""
    global _turns_since_check
    _turns_since_check += 1
    if _turns_since_check < _int_env("ROUTIVUS_ADAPTIVE_CHECK_ROUNDS", CHECK_ROUNDS):
        return
    _turns_since_check = 0
    maybe_spawn("rounds")


# ---------------------------------------------------------------------------
# 子进程入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SmartRouter 本地进化（子进程入口）")
    parser.add_argument("--trigger", default="cli", help="触发来源：startup / rounds / manual / cli")
    parser.add_argument("--force", action="store_true", help="跳过门槛（仍守不劣门）")
    args = parser.parse_args(argv)

    if not auto_enabled() and args.trigger != "manual":
        print(json.dumps({"decision": "skipped", "reason": "disabled"}, ensure_ascii=False))
        return 0
    result = run_evolve(args.trigger, force=args.force)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
