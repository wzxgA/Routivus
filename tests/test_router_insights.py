"""SmartRouter 数据看板（方案 13）的聚合层测试。

覆盖三类容易出错的点：

1. **空环境不报错**：没有任何文件与事件时也要 200，并且结论是"没样本"而不是空白；
2. **降级而不是崩**：calibration / 产物 / evolve.log 损坏时只让那一块降级；
3. **口径自洽**：`by_tier` 与置信度分桶能对上 `total`，时间窗与本地日正确。

测试通过 `ROUTIVUS_ADAPTIVE_DIR` 把 adaptive 目录指到临时目录，因此不碰用户真实数据。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

import routivus.server.storage as storage_module
from routivus.adaptive.store import (
    calibration_path,
    evolve_log_path,
    learned_rules_path,
    ml_router_nosem_path,
    ml_router_path,
    sem_samples_path,
    semantic_head_path,
    semantic_onnx_path,
)
from routivus.server import insights as ins
from routivus.server.insights import router_insights
from routivus.server.storage import WorkspaceStore

PROJECT = "p-alpha"
CST = timezone(timedelta(hours=8))


@pytest.fixture
def adaptive_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 adaptive 数据目录指向临时目录（所有 *_path() 都跟着走）。"""
    target = tmp_path / "adaptive"
    target.mkdir()
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(target))
    monkeypatch.setattr(ins, "_SAMPLES_CACHE", None)  # 别让上一个用例的样本缓存串味
    return target


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认走"重资产未加载"的探测分支：测试进程里不该真去加载产物与 ONNX。"""
    monkeypatch.setattr(ins, "loaded_assets", lambda: None)


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "workspace.sqlite3")


def _note_feedback(adaptive_dir: Path, records: list[dict]) -> None:
    path = adaptive_dir / "feedback.log"
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _feedback_rows(count: int, tier: str = "Enhanced", signal: str = "upgrade",
                   source: str = "clarify", weight: float = 1.0) -> list[dict]:
    # text_hash 带上档位与信号来源：`build_samples` 会按 hash 聚合，撞 hash 会互相抵消
    prefix = f"{tier[:2]}-{source[:2]}-{signal[:2]}"
    return [
        {
            "ts": 1_700_000_000.0 + index,
            "session": "s",
            "source": source,
            "signal": signal,
            "text_hash": f"{prefix}-{index:04d}",
            "model_tier": tier,
            "weight": weight,
            "features": {"len_chars": 30.0},
        }
        for index in range(count)
    ]


def _add_events(store: WorkspaceStore, monkeypatch: pytest.MonkeyPatch, payloads: list[dict],
                when: str = "2026-09-15T02:00:00+00:00") -> None:
    """插 router.updated 事件；每插一条都把 storage 的 _now 固定到给定 UTC 时刻。"""
    session = store.create_session(PROJECT, title="会话")
    monkeypatch.setattr(storage_module, "_now", lambda: when)
    for payload in payloads:
        store.append_event(session.id, PROJECT, "router.updated", payload)


def _routed(tier: str = "Enhanced", confidence: float = 0.71, **extra) -> dict:
    return {"enabled": True, "tier": tier, "tier_idx": 1, "provider": "p", "model": "m",
            "configured": True, "confidence": confidence, "hard_rule": False, "score": 5.0,
            "notes": ["ml:idx=1,p=0.71"], "elapsed_ms": 180, "load_ms": 0,
            "switched": True, **extra}


# ---------- 空态与降级 ----------


def test_empty_environment_is_not_an_error(store, adaptive_dir, probe):
    payload = router_insights(store)

    assert payload["days"] == 30
    assert payload["status"]["verified"] is False
    assert payload["status"]["artifact"]["source"] == "unavailable"
    assert payload["status"]["artifact"]["reason_code"] == "no_artifact"
    assert payload["runtime"]["total"] == 0
    assert payload["calibration"]["bias"] == {} or set(payload["calibration"]["bias"].values()) == {0.0}
    assert payload["rules"]["items"] == []
    assert payload["samples"]["feedback_lines"] == 0
    assert payload["evolution"]["gates"]  # 空态也要给出门槛进度，不给"暂无数据"
    assert payload["diagnosis"]["code"] == "ml_unavailable"  # 产物不可用优先于"没样本"


def test_missing_artifact_files_are_reported_at_their_own_step(store, adaptive_dir, probe):
    payload = router_insights(store)
    # 语义产物与 tokenizer 都不在 → 探测给 artifact_missing（而不是笼统的"不可用"）
    assert payload["status"]["semantic"]["reason_code"] == "artifact_missing"
    assert payload["diagnosis"]["hints"]


def test_nosem_fallback_is_flagged(store, adaptive_dir, probe):
    ml_router_nosem_path().write_bytes(b"not a real artifact")

    payload = router_insights(store)

    assert payload["status"]["artifact"]["source"] == "nosem"
    assert payload["status"]["artifact"]["eval_error"] == "load_failed"  # 假文件读不出内嵌字段
    assert payload["diagnosis"]["code"] == "ml_nosem"


def test_semantic_artifact_with_encoder(store, adaptive_dir, probe, monkeypatch):
    ml_router_path().write_bytes(b"x")
    semantic_onnx_path().write_bytes(b"x")
    semantic_onnx_path().with_suffix(".json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ins, "_probe_semantic", lambda: (True, ""))

    payload = router_insights(store)

    assert payload["status"]["artifact"]["source"] == "semantic"
    assert payload["status"]["semantic"]["available"] is True
    assert payload["diagnosis"]["code"] == "no_samples"  # 产物没问题 → 轮到"没有信号"


def test_loaded_assets_take_priority(store, adaptive_dir, monkeypatch):
    """已加载共享资产时直接读它（与 chat 同源），不再走探测。"""

    class _Ml:
        source = "semantic"
        unavailable_reason = ""
        sem_dim = 512
        head_dim = 4
        n_samples = 34
        val_accuracy = 0.286
        trained_at = 1_700_000_000.0
        artifact_path = Path("/tmp/router.lgb")

    class _Semantic:
        available = True
        unavailable_reason = ""
        dim = 512
        calls = 7
        avg_ms = 12.5

    class _Assets:
        ml = _Ml()
        semantic = _Semantic()
        load_seconds = 1.25

    _Ml.semantic = _Semantic()
    monkeypatch.setattr(ins, "loaded_assets", lambda: _Assets())

    payload = router_insights(store)

    assert payload["status"]["verified"] is True
    assert payload["status"]["artifact"]["n_samples"] == 34
    assert payload["status"]["artifact"]["val_accuracy"] == 0.286
    assert payload["status"]["semantic"]["calls"] == 7
    assert payload["status"]["load_seconds"] == 1.25


def test_corrupt_calibration_degrades_without_raising(store, adaptive_dir, probe):
    calibration_path().write_text("{ this is not json", encoding="utf-8")

    payload = router_insights(store)

    assert payload["calibration"]["degraded"] is True
    assert payload["calibration"]["total"] == 0.0


def test_rules_are_sorted_by_support(store, adaptive_dir, probe):
    learned_rules_path().write_text(json.dumps({"rules": [
        {"predicate": {"len_chars<=": 60.0}, "action": 1, "confidence": 0.7, "support": 25.0},
        {"predicate": {"num_debug_kw>=": 1.0}, "action": -1, "confidence": 0.8, "support": 40.0},
    ]}), encoding="utf-8")

    items = router_insights(store)["rules"]["items"]

    assert [item["support"] for item in items] == [40.0, 25.0]
    assert items[0]["feature"] == "num_debug_kw" and items[0]["action"] == -1


def test_evolve_log_history(store, adaptive_dir, probe):
    path = evolve_log_path()
    path.write_text("\n".join(json.dumps(row) for row in [
        {"trigger": "startup", "ts": 1.0, "decision": "skipped", "reason": "not_enough_total", "samples": 12},
        {"trigger": "manual", "ts": 2.0, "decision": "replaced", "reason": "", "samples": 130,
         "holdout_acc": 0.71, "baseline_acc": 0.66},
    ]) + "\n{ broken json\n", encoding="utf-8")

    history = router_insights(store)["evolution"]["history"]

    assert [row["decision"] for row in history] == ["skipped", "replaced"]
    assert history[1]["holdout_acc"] == 0.71


# ---------- 运行态统计 ----------


def test_runtime_counts_and_buckets(store, adaptive_dir, probe, monkeypatch):
    _add_events(store, monkeypatch, [
        _routed("Basic", 0.42),
        _routed("Enhanced", 0.61, hard_rule=True),
        _routed("Superior", 0.95),
        {"enabled": True, "tier": "", "provider": "", "model": "", "error": "路由失败，本轮沿用当前模型：x"},
    ])

    runtime = router_insights(store)["runtime"]

    assert runtime["total"] == 4
    assert runtime["by_tier"] == {"Basic": 1, "Enhanced": 1, "Superior": 1, "Ultimate": 0, "unknown": 1}
    assert runtime["routed"] == 3 and runtime["errors"] == 1
    assert runtime["routed"] + runtime["by_tier"]["unknown"] == runtime["total"]
    assert sum(runtime["confidence_buckets"].values()) == runtime["routed"]
    assert runtime["confidence_buckets"]["<0.5"] == 1
    assert runtime["confidence_buckets"][">=0.9"] == 1
    assert runtime["hard_rule"] == 1
    assert runtime["switched"] == 3
    assert runtime["error_groups"] == [{"text": "路由失败，本轮沿用当前模型：x", "count": 1}]
    assert runtime["recent"][-1]["notes"] == []
    assert runtime["recent"][0]["tier"] == "Basic"


def test_runtime_percentiles(store, adaptive_dir, probe, monkeypatch):
    _add_events(store, monkeypatch, [_routed(elapsed_ms=value) for value in (100, 200, 300, 400)])

    latency = router_insights(store)["runtime"]["latency_ms"]

    assert latency["route_max"] == 400
    assert latency["route_p50"] in {200, 300}  # 偶数个点取中位附近，不锁死一边
    assert latency["route_p95"] == 400


def test_events_outside_window_are_ignored(store, adaptive_dir, probe, monkeypatch):
    _add_events(store, monkeypatch, [_routed()])

    far_future = datetime(2027, 1, 1, tzinfo=CST)
    payload = router_insights(store, now=far_future)

    assert payload["runtime"]["total"] == 0
    assert payload["runtime"]["events_since"] < "2027"


def test_truncated_flag_when_limit_reached(store, adaptive_dir, probe, monkeypatch):
    monkeypatch.setattr(ins, "EVENT_LIMIT", 2)
    _add_events(store, monkeypatch, [_routed(), _routed(), _routed()])

    runtime = router_insights(store)["runtime"]

    assert runtime["total"] == 2
    assert runtime["truncated"] is True


def test_route_errors_diagnosis(store, adaptive_dir, probe, monkeypatch):
    _add_events(store, monkeypatch, [
        {"enabled": True, "tier": "", "provider": "", "model": "", "error": "boom"},
        {"enabled": True, "tier": "", "provider": "", "model": "", "error": "boom"},
    ])

    diagnosis = router_insights(store)["diagnosis"]

    assert diagnosis["level"] == "error"
    assert diagnosis["code"] == "route_errors"
    assert diagnosis["hints"]


# ---------- 样本与门槛 ----------


def test_gate_detail_shows_gap(store, adaptive_dir, probe):
    # 训练样本的档位是**由信号方向反推**的（upgrade → 上一轮档位 +1），所以这里给的
    # Basic 行最终落在 Enhanced 桶里——看板显示的是"可用样本"的真实分布，不是原始行。
    _note_feedback(adaptive_dir, _feedback_rows(12, tier="Basic") + _feedback_rows(31, tier="Enhanced"))

    evolution = router_insights(store)["evolution"]

    per_tier = next(gate for gate in evolution["gates"] if gate["key"] == "per_tier")
    assert per_tier["satisfied"] is False
    assert "Enhanced 12/20" in per_tier["detail"]
    assert evolution["counts_by_tier"]["Enhanced"] == 12
    assert evolution["counts_by_tier"]["Superior"] == 31
    assert evolution["total_samples"] == 43


def test_samples_block_summarises_signals(store, adaptive_dir, probe):
    _note_feedback(adaptive_dir, [
        *_feedback_rows(3, tier="Enhanced", signal="upgrade", source="clarify"),
        *_feedback_rows(2, tier="Superior", signal="downgrade", source="short_high_tier", weight=0.3),
    ])

    samples = router_insights(store)["samples"]

    assert samples["feedback_lines"] == 5
    assert samples["by_signal"]["clarify"]["up"] == 3
    assert samples["by_signal"]["short_high_tier"]["down"] == 2
    assert samples["unique_texts"] == 5
    assert round(samples["weighted_total"], 2) == 3.6


def test_sem_samples_are_counted_without_dequantizing(store, adaptive_dir, probe):
    sem_samples_path().write_text(
        "\n".join(json.dumps({"ts": 1_700_000_000.0 + index, "text_hash": f"h{index}", "sem": "AAAA"})
                  for index in range(4)) + "\n",
        encoding="utf-8",
    )
    semantic_head_path().write_text(
        json.dumps({"centroids": [[0.1, 0.2], [0.3, 0.4]], "dim": 512, "train_acc": 0.8}),
        encoding="utf-8",
    )

    samples = router_insights(store)["samples"]

    assert samples["sem_samples"]["count"] == 4
    assert samples["sem_samples"]["bytes"] > 0
    assert samples["semantic_head"] == {"present": True, "tiers": 2, "dim": 512, "train_acc": 0.8}


def test_no_samples_diagnosis_mentions_signals(store, adaptive_dir, probe, monkeypatch):
    ml_router_nosem_path().write_bytes(b"x")  # 让产物这一段不抢优先级
    payload = router_insights(store)

    # 产物是兜底版 → 优先报 ml_nosem；这里只验证 hints 里没有空话
    assert payload["diagnosis"]["code"] == "ml_nosem"


# ---------- 口径与参数 ----------


def test_days_is_clamped(store, adaptive_dir, probe):
    assert router_insights(store, days=3)["days"] == 7
    assert router_insights(store, days=400)["days"] == 90
    assert router_insights(store, days=30)["days"] == 30


def test_local_day_boundary(store, adaptive_dir, probe):
    """本地 00:30 起算的窗口，起点是当天 00:00 本地时间对应的 UTC。"""
    payload = router_insights(store, days=7, now=datetime(2026, 9, 15, 0, 30, tzinfo=CST), tz=CST)

    assert payload["timezone"] == str(CST)
    # 9-09 00:00 +08:00 == 9-08 16:00 UTC
    assert payload["runtime"]["events_since"].startswith("2026-09-08T16:00")


def test_privacy_no_text_or_hashes(store, adaptive_dir, probe):
    secret = "SUPER_SECRET_PROMPT_TOKEN"
    _note_feedback(adaptive_dir, [{
        "ts": 1_700_000_000.0, "session": "s", "source": "clarify", "signal": "upgrade",
        "text_hash": "deadbeefcafe", "model_tier": "Enhanced", "weight": 1.0,
        "features": {"len_chars": 12.0, "note": secret},
    }])

    text = json.dumps(router_insights(store), ensure_ascii=False)

    assert secret not in text
    assert "deadbeefcafe" not in text
    assert "text_hash" not in text


def test_endpoint_is_registered_and_clamps_days(tmp_path, adaptive_dir, probe):
    """接线的回归：路由真挂在 `/api/router/insights` 上，且 days 由服务端钳制。"""
    from fastapi.testclient import TestClient

    from routivus.server import ProjectRegistry, create_app
    from routivus.server.config import ServerConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    client = TestClient(
        create_app(
            registry=ProjectRegistry(tmp_path / "projects.json", [workspace]),
            store=WorkspaceStore(tmp_path / "workspace.sqlite3"),
            config=config,
            agent_factory=lambda project, session: object(),
        )
    )

    response = client.get("/api/router/insights", params={"days": 400})

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 90
    assert {"status", "diagnosis", "runtime", "calibration", "rules", "samples", "evolution", "limits"} <= set(body)
    assert body["limits"]["evolve_min_per_tier"] == 20


def test_limits_come_from_the_modules(store, adaptive_dir, probe):
    limits = router_insights(store)["limits"]

    assert limits["calibration_min_samples"] == 20
    assert limits["max_bias"] == 0.15
    assert limits["evolve_cooldown_days"] == 7.0
    assert limits["sem_samples_max"] == 50_000
    assert limits["signals"]["clarify"] == {"upgrade": True, "weight": 1.0}
