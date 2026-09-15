"""Token 用量统计（方案 12）的存储级与协议级测试。

重点验三件事：迁移、**两条口径的自洽**（逐轮事件 vs 会话累计快照）、本地日边界。
时区与"现在"一律注入固定值，不依赖跑测试机器的时区——否则东八区的开发者与 UTC 的
CI 会得到不同结果。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore

TZ = timezone(timedelta(hours=8))  # 固定东八区（也是这次时区修正的动机）
NOW = datetime(2026, 9, 15, 21, 0, tzinfo=TZ)  # 本地 2026-09-15 21:00


def _store(tmp_path: Path) -> tuple[WorkspaceStore, Path]:
    path = tmp_path / "workspace.sqlite3"
    return WorkspaceStore(path), path


def _utc(local: datetime) -> str:
    """本地时间 → 与 `_now()` 同格式的 UTC ISO 串（两边要能直接比字符串）。"""
    return local.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _insert_event(
    path: Path,
    session_id: str,
    project_id: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, object] | None = None,
) -> None:
    """直接插事件：`append_event` 用的是当前时间，而这里必须精确控制时间戳。"""
    conn = sqlite3.connect(path)
    try:
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO events (event_id, sequence, session_id, project_id, event_type, data_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, sequence, session_id, project_id, event_type, json.dumps(payload or {}), _utc(occurred_at)),
        )
        conn.commit()
    finally:
        conn.close()


def _usage(
    path: Path,
    session_id: str,
    project_id: str,
    occurred_at: datetime,
    *,
    prompt: int = 0,
    completion: int = 0,
    total: int = 0,
    tier: str = "",
) -> None:
    payload: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if tier:
        payload["tier"] = tier  # 方案 12 §4.2 之后的事件才有
    _insert_event(path, session_id, project_id, "session.usage", occurred_at, payload)


def _indexes(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


# ==========================================================================
# 1. 迁移
# ==========================================================================


def test_fresh_database_migrates_to_current_version(tmp_path: Path) -> None:
    WorkspaceStore(tmp_path / "workspace.sqlite3")
    path = tmp_path / "workspace.sqlite3"

    assert _user_version(path) == 4
    assert "idx_events_type_time" in _indexes(path)


def test_old_database_gets_usage_index_on_open(tmp_path: Path) -> None:
    """老库（user_version=3、没有新索引）打开后补索引并升到 4：纯向上迁移。"""
    store, path = _store(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_events_type_time")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    reopened = WorkspaceStore(path)

    assert reopened.VERSION == 4
    assert _user_version(path) == 4
    assert "idx_events_type_time" in _indexes(path)


# ==========================================================================
# 2. 两条口径自洽
# ==========================================================================


def test_event_sum_agrees_with_session_totals(tmp_path: Path) -> None:
    """逐轮事件之和 == 会话累计快照（对账 drift 为 0）——这条同时守住"不会双计"。"""
    store, path = _store(tmp_path)
    session = store.create_session("p1", "s")
    _usage(path, session.id, "p1", NOW - timedelta(hours=1), prompt=100, completion=20, total=120, tier="Superior")
    _usage(path, session.id, "p1", NOW - timedelta(hours=2), prompt=200, completion=40, total=240, tier="Basic")
    store.update_session(session.id, prompt_tokens=300, completion_tokens=60, total_tokens=360)

    summary = store.usage_summary(days=30, tz=TZ, now=NOW)

    assert summary["today"] == {"prompt": 300, "completion": 60, "total": 360, "turns": 2}
    assert summary["range"]["total"] == 360
    assert summary["lifetime"] == {"prompt": 300, "completion": 60, "total": 360, "sessions": 1}
    assert summary["drift"] == {"events_total": 360, "sessions_total": 360, "diff": 0, "diff_pct": 0.0}


def test_drift_reports_missing_events(tmp_path: Path) -> None:
    """只有会话快照、没有逐轮事件时（历史被裁过 / 老版本没发过 usage），差值是显式的。"""
    store, _ = _store(tmp_path)
    session = store.create_session("p1", "s")
    store.update_session(session.id, prompt_tokens=0, completion_tokens=0, total_tokens=500)

    summary = store.usage_summary(days=30, tz=TZ, now=NOW)

    assert summary["range"]["total"] == 0
    assert summary["lifetime"]["total"] == 500
    assert summary["drift"]["events_total"] == 0
    assert summary["drift"]["diff"] == -500
    assert summary["drift"]["diff_pct"] == 100.0


# ==========================================================================
# 3. 按天序列与本地日边界
# ==========================================================================


def test_daily_series_fills_empty_days_and_equals_range(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    session = store.create_session("p1", "s")
    _usage(path, session.id, "p1", NOW, total=10)
    _usage(path, session.id, "p1", NOW - timedelta(days=3), total=5)

    summary = store.usage_summary(days=7, tz=TZ, now=NOW)

    daily = summary["daily"]
    assert [item["date"] for item in daily] == [f"2026-09-{day:02d}" for day in range(9, 16)]
    assert daily[-1]["total"] == 10 and daily[-1]["turns"] == 1
    assert daily[-4]["date"] == "2026-09-12" and daily[-4]["total"] == 5
    assert daily[0]["total"] == 0  # 空日子补 0，折线的 x 轴才完整
    assert sum(item["total"] for item in daily) == summary["range"]["total"] == 15


def test_usage_is_grouped_by_local_day(tmp_path: Path) -> None:
    """本地 00:30 与 23:30 算同一天；前一天的 23:30 不算今天。

    按 UTC 分组的话，本地 00:30 那条（= 前一天 16:30 UTC）会跑到前一天去。
    """
    store, path = _store(tmp_path)
    session = store.create_session("p1", "s")
    _usage(path, session.id, "p1", datetime(2026, 9, 15, 0, 30, tzinfo=TZ), total=1)
    _usage(path, session.id, "p1", datetime(2026, 9, 15, 23, 30, tzinfo=TZ), total=2)
    _usage(path, session.id, "p1", datetime(2026, 9, 14, 23, 30, tzinfo=TZ), total=4)

    summary = store.usage_summary(days=7, tz=TZ, now=NOW)

    by_date = {item["date"]: item["total"] for item in summary["daily"]}
    assert by_date["2026-09-15"] == 3
    assert by_date["2026-09-14"] == 4
    assert summary["today"]["total"] == 3


# ==========================================================================
# 4. 归因：档位与项目
# ==========================================================================


def test_tier_attribution_flags_unlabeled_events(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    session = store.create_session("p1", "s")
    _usage(path, session.id, "p1", NOW, total=100, tier="Superior")
    _usage(path, session.id, "p1", NOW, total=30)  # 老事件：没有 tier 字段

    summary = store.usage_summary(days=7, tz=TZ, now=NOW)

    tiers = {item["tier"]: item for item in summary["by_tier"]}
    assert tiers["Superior"] == {"tier": "Superior", "total": 100, "turns": 1}
    assert tiers["未标注"] == {"tier": "未标注", "total": 30, "turns": 1}
    assert summary["by_tier"][0]["tier"] == "Superior"  # 按 total 降序


def test_by_project_splits_range_and_lifetime(tmp_path: Path) -> None:
    """区间内用量来自事件，累计来自会话快照；范围外的事件只进累计与对账。"""
    store, path = _store(tmp_path)
    first = store.create_session("p1", "s1")
    second = store.create_session("p2", "s2")
    _usage(path, first.id, "p1", NOW, total=100)
    _usage(path, second.id, "p2", NOW, total=7)
    _usage(path, first.id, "p1", NOW - timedelta(days=40), total=999)  # 30 天窗口外
    store.update_session(first.id, prompt_tokens=1099, completion_tokens=0, total_tokens=1099)
    store.update_session(second.id, prompt_tokens=7, completion_tokens=0, total_tokens=7)

    summary = store.usage_summary(days=30, tz=TZ, now=NOW)

    projects = {item["project_id"]: item for item in summary["by_project"]}
    assert projects["p1"]["range"] == 100 and projects["p1"]["lifetime"] == 1099
    assert projects["p2"]["today"] == 7 and projects["p2"]["turns"] == 1
    assert summary["lifetime"]["total"] == 1106
    assert summary["drift"]["diff"] == 0  # 全量事件也含窗口外那条


def test_days_is_clamped(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.usage_summary(days=3, tz=TZ, now=NOW)["days"] == 7
    assert store.usage_summary(days=0, tz=TZ, now=NOW)["days"] == 7
    assert store.usage_summary(days=400, tz=TZ, now=NOW)["days"] == 90


# ==========================================================================
# 5. 时区修正：今日调用与热力图
# ==========================================================================


def test_calls_today_and_activity_use_local_day(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    session = store.create_session("p1", "s")
    _insert_event(path, session.id, "p1", "tool.started", datetime(2026, 9, 15, 0, 30, tzinfo=TZ))
    _insert_event(path, session.id, "p1", "tool.started", datetime(2026, 9, 14, 23, 30, tzinfo=TZ))

    assert store.project_stats("p1", tz=TZ, now=NOW)["calls_today"] == 1

    by_day = {item["date"]: item for item in store.activity(tz=TZ)}
    assert by_day["2026-09-15"]["count"] == 1
    assert by_day["2026-09-14"]["count"] == 1
    assert by_day["2026-09-15"]["projects"] == {"p1": 1}


def test_activity_rejects_bad_date(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with pytest.raises(ValueError):
        store.activity("2026-13-99")


# ==========================================================================
# 6. 接口
# ==========================================================================


def test_usage_summary_endpoint_reports_project_names(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    client = TestClient(
        create_app(registry=registry, store=store, config=config, agent_factory=lambda project, session: object())
    )
    root = workspace / "alpha"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "Alpha", "root_path": str(root)}).json()
    client.post(f"/api/projects/{project['id']}/sessions", json={"title": "s"})

    response = client.get("/api/usage/summary", params={"days": 30})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {"timezone", "days", "today", "range", "lifetime", "daily", "by_project", "by_tier", "drift"}
    assert payload["days"] == 30 and len(payload["daily"]) == 30
    entry = {item["project_id"]: item for item in payload["by_project"]}[project["id"]]
    assert entry["name"] == "Alpha" and entry["lifetime"] == 0


def test_activity_endpoint_rejects_bad_date(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    client = TestClient(create_app(registry=registry, config=ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )))

    response = client.get("/api/activity", params={"from": "not-a-date"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_date"
