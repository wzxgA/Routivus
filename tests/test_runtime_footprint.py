"""运行时资源占用的回归线（方案 plans/Optimization/01-runtime-resource-footprint.md）。

覆盖两件事，都是"改了没人会发现、坏了却会静默退化"的那类：

1. **流式增量只广播、不落库**（§5.1）。delta 占事件表条数的 73%，是磁盘 IOPS 的主因；
   它不被重连回放、前端也只实时消费，所以可以完全不落库。**但它必须仍然广播到 WS** ——
   这是本文件最重要的一条断言：省掉写入不能连推送给一起省掉。
2. **共享长连接**（§5.2）。每次访问新建连接的固定开销占单次事务的 62%；改成复用后，
   连接必须在多次调用间保持同一个，且失效 / 被关闭后能自动重建而不会让后续请求全线失败。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


class _ScriptedAgent:
    """按脚本回放 AgentEvent 的假 agent（够 llm / tools / settings 三个依赖）。"""

    def __init__(self, script: list[AgentEvent]) -> None:
        self._script = script
        self.llm = object()
        self.tools = object()
        self.settings = object()
        self.approval_policy = None
        self.ask_requester = None

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        for event in self._script:
            yield event
        yield AgentEvent(kind="done")


def _client(tmp_path: Path) -> tuple[TestClient, WorkspaceStore]:
    """建 app 并把 store 一起交出来（断言要看落库结果）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    client = TestClient(
        create_app(
            registry=registry,
            store=store,
            config=config,
            # agent_factory 的调用签名是 (project, session)
            agent_factory=lambda *_, **__: _ScriptedAgent([AgentEvent(kind="content", text="reply: hi")]),
        )
    )
    return client, store


def _run_turn(client: TestClient) -> tuple[dict, list[dict]]:
    """跑一轮脚本化对话，返回 (会话, 收到的全部 WS 事件)。"""
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / "alpha"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "Alpha", "root_path": str(root)}).json()
    session = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"}).json()
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        # snapshot 也留住：它是序号基准（前端靠它的 sequence 重置 lastSequence）
        events: list[dict] = [socket.receive_json()]
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "hello"})
        for _ in range(120):
            envelope = socket.receive_json()
            events.append(envelope)
            if envelope.get("type") == "session.status" and envelope.get("data", {}).get("status") == "completed":
                break
    return session, events


# ==========================================================================
# §5.1 流式增量只广播、不落库
# ==========================================================================


def test_delta_is_broadcast_but_not_persisted(tmp_path: Path) -> None:
    """核心断言：WS 收得到 delta，事件表里却一条都没有。"""
    client, store = _client(tmp_path)
    session, events = _run_turn(client)

    deltas = [item for item in events if item.get("type") == "message.delta"]
    assert deltas, "delta 必须仍然广播 —— 省掉的是写入，不是推送"
    assert deltas[0]["data"]["text"] == "reply: hi"

    assert store.count_events(session["id"], "message.delta") == 0


def test_ephemeral_payload_carries_no_sequence(tmp_path: Path) -> None:
    """瞬时事件不带 sequence：前端据此跳过 lastSequence 推进（它不占号）。"""
    client, _ = _client(tmp_path)
    _, events = _run_turn(client)

    deltas = [item for item in events if item.get("type") == "message.delta"]
    assert deltas
    assert all(item.get("sequence") is None for item in deltas)
    # 载荷其余字段与常规事件对齐，前端消费路径无需分支
    assert all(item.get("session_id") and item.get("occurred_at") for item in deltas)


def test_persisted_events_still_advance_sequence(tmp_path: Path) -> None:
    """delta 不占号后，真正落库的事件序号仍严格递增且大于快照序号。

    这是前端去重逻辑的前提：`session.snapshot` 重置 lastSequence，此后只接受严格
    递增的 sequence。delta 缺席不能影响这条链。
    """
    client, _ = _client(tmp_path)
    _, events = _run_turn(client)

    snapshot = next(item for item in events if item.get("type") == "session.snapshot")
    assert isinstance(snapshot["sequence"], int) and snapshot["sequence"] > 0

    persisted = [
        item["sequence"]
        for item in events
        if item.get("type") in {"message.segment", "tool.started", "tool.completed", "session.status"}
    ]
    assert persisted, "本轮应当至少有段落 / 状态类事件落库"
    assert all(seq is not None and seq > snapshot["sequence"] for seq in persisted)
    assert persisted == sorted(persisted)


def test_segment_still_persists_body(tmp_path: Path) -> None:
    """正文的持久化没有受影响：delta 不落库，但段落照旧进 messages 表。"""
    client, _ = _client(tmp_path)
    session, events = _run_turn(client)

    segments = [item for item in events if item.get("type") == "message.segment"]
    assert segments and segments[0]["data"]["message"]["content"] == "reply: hi"

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "reply: hi"


# ==========================================================================
# §5.2 共享长连接
# ==========================================================================


def test_connection_is_reused_across_calls(tmp_path: Path) -> None:
    """多次读写之间保持同一个连接对象——这正是省掉那 62% 固定开销的地方。"""
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")

    first = store._connect()
    for _ in range(5):
        store.append_event(session.id, session.project_id, "tool.started", {"n": 1})
        store.latest_event_sequence(session.id)
        store.list_events(session.id)

    assert store._connect() is first
    assert store.count_events(session.id, "tool.started") == 5


def test_close_then_access_rebuilds_connection(tmp_path: Path) -> None:
    """close() 后仍可继续使用（自动重建），不会把 store 变成废对象。"""
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    before = store._connect()

    store.close()
    after = store._connect()

    assert after is not before
    # 关闭只是断开句柄，数据仍在
    assert store.get_session(session.id) is not None
    store.close()


def test_externally_closed_connection_is_rebuilt(tmp_path: Path) -> None:
    """连接被外部关掉（磁盘错误 / sqlite 内部失效）时，下一次访问应自动重建。"""
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    dead = store._connect()
    dead.close()  # 绕过 store.close()，模拟"连接坏了但 store 不知道"

    rebuilt = store._connect()
    assert rebuilt is not dead
    assert store.get_session(session.id) is not None
    store.close()


def test_count_tool_failures_fallback_without_json1(tmp_path: Path) -> None:
    """json1 不可用时的 Python 回退路径仍可走通（该分支已改为不在连接块内嵌套）。

    嵌套 `with self._connect()` 在共享长连接下会让内层退出时提前提交外层事务，
    所以这条回退必须在连接块之外执行。
    """
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    store.append_event(session.id, session.project_id, "tool.completed", {"ok": False})
    store.append_event(session.id, session.project_id, "tool.completed", {"ok": True})

    real = store._connect()

    class _NoJson1:
        """转发给真实连接，但对 json_extract 抛错（模拟编译时未启用 json1）。"""

        def __enter__(self) -> "_NoJson1":
            real.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return real.__exit__(*exc)

        def execute(self, sql: str, *args: Any) -> Any:
            if "json_extract" in sql:
                raise sqlite3.OperationalError("no such function: json_extract")
            return real.execute(sql, *args)

        def close(self) -> None:
            real.close()

    store._conn = _NoJson1()  # type: ignore[assignment]
    assert store.count_tool_failures(session.id) == 1
    store.close()


# ==========================================================================
# §5.3 快照只占序号
# ==========================================================================


def test_snapshot_persists_only_placeholder(tmp_path: Path) -> None:
    """落库的那份是空占位：快照正文（含 replay，可达数百 KB）只发给前端。"""
    client, store = _client(tmp_path)
    session, _ = _run_turn(client)

    records = store.list_card_events(session["id"], ("session.snapshot",))
    assert len(records) == 1
    assert records[0].data == {}


def test_snapshot_pushed_to_ws_is_intact(tmp_path: Path) -> None:
    """WS 收到的仍是完整快照——只占序号不能把推给前端的内容一起省掉。"""
    client, _ = _client(tmp_path)
    _, events = _run_turn(client)

    snapshot = events[0]
    assert snapshot["type"] == "session.snapshot"
    data = snapshot["data"]
    assert isinstance(data["last_sequence"], int) and data["last_sequence"] > 0
    assert "replay" in data and "audit" in data and "pending" in data


# ==========================================================================
# §5.4 删除冗余索引
# ==========================================================================


def _event_index_names(store: WorkspaceStore) -> set[str]:
    with store._lock, store._connect() as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'events'"
            )
        }


def test_redundant_index_is_dropped_on_fresh_db(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    names = _event_index_names(store)

    assert "idx_events_session_sequence" not in names
    # 等价的那条 UNIQUE 约束索引还在（查询不会因此退化）
    assert any(name.startswith("sqlite_autoindex_events") for name in names)
    assert {"idx_events_project_time", "idx_events_type_time"} <= names
    store.close()


def test_migration_drops_redundant_index(tmp_path: Path) -> None:
    """模拟 v4 老库：带重复索引 + user_version=4，打开后应被迁移删掉。"""
    db = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL);
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        );
        CREATE INDEX idx_events_session_sequence ON events(session_id, sequence ASC);
        CREATE INDEX idx_events_project_time ON events(project_id, occurred_at ASC);
        CREATE INDEX idx_events_type_time ON events(event_type, occurred_at ASC);
        PRAGMA user_version = 4;
        """
    )
    legacy.commit()
    legacy.close()

    store = WorkspaceStore(db)
    assert (
        store._connect().execute("PRAGMA user_version").fetchone()[0]
        == WorkspaceStore.VERSION
    )
    names = _event_index_names(store)
    assert "idx_events_session_sequence" not in names
    assert {"idx_events_project_time", "idx_events_type_time"} <= names
    store.close()


def test_event_queries_still_use_index(tmp_path: Path) -> None:
    """删索引的验收线：按会话取事件的查询仍走索引，没有退化成全表扫描。"""
    store = WorkspaceStore(tmp_path / "ws.sqlite3")
    session = store.create_session("p1")
    store.append_event(session.id, session.project_id, "tool.started", {})

    with store._lock, store._connect() as conn:
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM events WHERE session_id = ? AND sequence > ?"
                " ORDER BY sequence ASC LIMIT ?",
                (session.id, 0, 500),
            )
        ).upper()

    assert "USING" in plan, plan
    assert "SCAN EVENTS" not in plan, plan
    store.close()
