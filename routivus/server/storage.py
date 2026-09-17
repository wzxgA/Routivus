"""SQLite persistence for Web Console sessions, messages and notes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4


SessionStatus = Literal["idle", "running", "waiting_approval", "completed", "failed", "cancelled"]
# thinking：模型推理段（仅展示用，方案 07 §4.1；不参与 agent 上下文重建）
MessageRole = Literal["user", "assistant", "tool", "system", "thinking"]
_MISSING = object()


class NoteConflictError(ValueError):
    """The note changed after the client loaded it."""


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    title: str
    status: str
    active_provider: str
    active_model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MessageRecord:
    id: str
    session_id: str
    role: str
    content: str
    tool_name: str | None
    tool_args: dict | None
    tool_result: str | None
    created_at: str


@dataclass(frozen=True)
class NoteRecord:
    id: str
    project_id: str | None
    title: str
    body_markdown: str
    tags: tuple[str, ...]
    pinned: bool
    created_at: str
    updated_at: str
    version: int


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    sequence: int
    session_id: str
    project_id: str
    event_type: str
    data: dict
    occurred_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------- 时区与用量（方案 12）----------

# 事件里的 occurred_at 一律是 UTC（`_now()`），但"今天 / 某一天"必须按**用户所在
# 时区**算：东八区用 UTC 日期分组，00:00-08:00 的活动会被算到前一天（热力图上"今天"
# 一直是冷的）。所以：范围过滤仍用 occurred_at 的 UTC 边界（走得上索引），**分日交给
# Python 转本地时区** —— SQLite 不认识本地时区，写死偏移在有夏令时的时区会错。


def _local_tz() -> tzinfo:
    """本机时区；取不到时退回 UTC（宁可差时区，也不要抛错）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _iso(moment: datetime) -> str:
    """与 `_now()` 同格式的 UTC ISO 串，两边能直接做字符串比较。"""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _utc_bounds(start: date, end_exclusive: date, tz: tzinfo) -> tuple[str, str]:
    """本地日期区间 [start, end_exclusive) → UTC 的 ISO 边界（含头不含尾）。"""
    begin = datetime.combine(start, time.min, tzinfo=tz).astimezone(timezone.utc)
    finish = datetime.combine(end_exclusive, time.min, tzinfo=tz).astimezone(timezone.utc)
    return _iso(begin), _iso(finish)


def _local_day(occurred_at: str, tz: tzinfo) -> str:
    """UTC ISO 时间戳 → 本地日期（YYYY-MM-DD）。解析不了就退回原串的前 10 位。"""
    try:
        moment = datetime.fromisoformat(occurred_at)
    except ValueError:
        return occurred_at[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date().isoformat()


def _usage_payload(data_json: str) -> dict[str, Any]:
    """`session.usage` 事件的载荷；坏 JSON 当空字典，绝不让统计接口报错。"""
    try:
        payload = json.loads(data_json or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _usage_tokens(payload: dict[str, Any]) -> dict[str, int]:
    """从载荷里取 prompt / completion / total，缺字段按 0（老事件或只回了一部分）。"""
    def _int(key: str) -> int:
        try:
            return max(0, int(payload.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    return {"prompt": _int("prompt_tokens"), "completion": _int("completion_tokens"), "total": _int("total_tokens")}


def _accumulate(bucket: dict[str, int], tokens: dict[str, int]) -> None:
    """把一次用量累加进一个桶（桶的键固定：prompt / completion / total / turns）。"""
    bucket["prompt"] += tokens["prompt"]
    bucket["completion"] += tokens["completion"]
    bucket["total"] += tokens["total"]
    bucket["turns"] += 1


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WorkspaceStore:
    """Thread-safe SQLite store for server-owned workspace data."""

    VERSION = 5

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self._lock = RLock()
        # 共享长连接（惰性建立，见 `_connect`）。全部数据访问都在 `self._lock` 串行化
        # 之下，所以单连接是安全的；每次访问新建连接的固定开销实测占单次事务的 62%
        # （9.0 ms → 3.4 ms，见 plans/Optimization/01-runtime-resource-footprint.md §5.2）。
        self._conn: sqlite3.Connection | None = None
        self._initialize()

    def _open(self) -> sqlite3.Connection:
        # check_same_thread=False：连接跨线程复用（创建它的可能是启动线程，用的是
        # 事件循环线程），互斥改由 self._lock 保证 —— 这也是它比 sqlite3 自带的
        # 线程校验更严格的地方（后者只保证"同一线程"，不保证"同时只有一个"）。
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _connect(self) -> sqlite3.Connection:
        """返回共享长连接。签名与"每次新建"时代一致，调用点无需改动。

        `with conn` 仍是事务上下文（正常退出 commit、异常 rollback），只是不再承担
        "关闭连接"的职责。**由此得出一条硬约束：不允许嵌套 `with self._connect()`**
        —— 内层退出时会把外层尚未提交的事务一并提交。原实现里唯一的嵌套
        （`count_tool_failures` 的 json1 回退分支）已改为在连接块之外执行。
        """
        conn = self._conn
        if conn is not None:
            try:
                # 一次纯内存探活：长连接若因磁盘错误 / 被外部关闭而失效，不重建的话
                # 后续每个请求都会连带失败。调用方此时已持 _lock，重建是串行的。
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                self._conn = None
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        self._conn = self._open()
        return self._conn

    def close(self) -> None:
        """关闭共享连接（进程退出时调用）。关闭后再访问会自动重建，不会失效。"""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.VERSION:
                raise RuntimeError("工作区数据库版本不受支持")
            if version < 1:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        status TEXT NOT NULL DEFAULT 'idle',
                        active_provider TEXT NOT NULL DEFAULT '',
                        active_model TEXT NOT NULL DEFAULT '',
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_project_updated
                        ON sessions(project_id, updated_at DESC, id DESC);

                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL DEFAULT '',
                        tool_name TEXT,
                        tool_args_json TEXT,
                        tool_result TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_messages_session_created
                        ON messages(session_id, created_at ASC, id ASC);

                    CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        body_markdown TEXT NOT NULL DEFAULT '',
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE INDEX IF NOT EXISTS idx_notes_scope_updated
                        ON notes(project_id, updated_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_notes_pinned_updated
                        ON notes(pinned DESC, updated_at DESC);
                    PRAGMA user_version = 1;
                    """
                )
                version = 1
            if version < 2:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        sequence INTEGER NOT NULL,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        project_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        data_json TEXT NOT NULL DEFAULT '{}',
                        occurred_at TEXT NOT NULL,
                        UNIQUE(session_id, sequence)
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_session_sequence
                        ON events(session_id, sequence ASC);
                    CREATE INDEX IF NOT EXISTS idx_events_project_time
                        ON events(project_id, occurred_at ASC);
                    PRAGMA user_version = 2;
                    """
                )
                version = 2
            if version < 3:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS processed_requests (
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        request_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, request_id)
                    );
                    PRAGMA user_version = 3;
                    """
                )
            if version < 4:
                # 首页 token 面板（方案 12）：按"事件类型 + 时间窗"取逐轮用量。
                # 原来的两个索引都对不上这个查询（(session_id, sequence) 不带时间、
                # (project_id, occurred_at) 不带事件类型），少了它就得扫全表。
                # 只加索引、不动表结构，所以是纯向上迁移。
                conn.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_events_type_time
                        ON events(event_type, occurred_at ASC);
                    PRAGMA user_version = 4;
                    """
                )
            if version < 5:
                # 删掉与 UNIQUE(session_id, sequence) 完全重复的索引（Optimization 01 §5.4）：
                # 每次 INSERT 都要多维护一棵 B-tree，而 events 上的几个查询都不依赖它 ——
                # list_events / latest_event_sequence 走那条 UNIQUE，list_card_events 走
                # UNIQUE + 类型过滤，list_events_by_type 走 idx_events_type_time。
                # 只删索引、不动表结构，纯向上迁移。注意新库会在 v2 建它、这里再删掉
                # （多一次空表建索引的无用功），那是为了让 v2 的历史定义保持可追溯。
                conn.executescript(
                    """
                    DROP INDEX IF EXISTS idx_events_session_sequence;
                    PRAGMA user_version = 5;
                    """
                )

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=str(row["id"]), project_id=str(row["project_id"]), title=str(row["title"]),
            status=str(row["status"]), active_provider=str(row["active_provider"]),
            active_model=str(row["active_model"]), prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]), total_tokens=int(row["total_tokens"]),
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> MessageRecord:
        raw_args = row["tool_args_json"]
        try:
            args = json.loads(raw_args) if raw_args else None
        except (TypeError, json.JSONDecodeError):
            args = None
        return MessageRecord(
            id=str(row["id"]), session_id=str(row["session_id"]), role=str(row["role"]),
            content=str(row["content"]), tool_name=row["tool_name"], tool_args=args,
            tool_result=row["tool_result"], created_at=str(row["created_at"]),
        )

    @staticmethod
    def _note(row: sqlite3.Row) -> NoteRecord:
        try:
            raw_tags = json.loads(str(row["tags_json"]))
            tags = tuple(str(tag) for tag in raw_tags if str(tag).strip()) if isinstance(raw_tags, list) else ()
        except json.JSONDecodeError:
            tags = ()
        return NoteRecord(
            id=str(row["id"]), project_id=row["project_id"], title=str(row["title"]),
            body_markdown=str(row["body_markdown"]), tags=tags, pinned=bool(row["pinned"]),
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
            version=int(row["version"]),
        )

    # ---------- Sessions ----------

    def create_session(
        self,
        project_id: str,
        title: str = "新建会话",
        active_provider: str = "",
        active_model: str = "",
    ) -> SessionRecord:
        title = title.strip() or "新建会话"
        if len(title) > 200:
            raise ValueError("会话标题不能超过 200 个字符")
        record = SessionRecord(
            id=uuid4().hex, project_id=project_id, title=title, status="idle",
            active_provider=active_provider, active_model=active_model,
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            created_at=_now(), updated_at=_now(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO sessions
                (id, project_id, title, status, active_provider, active_model,
                 prompt_tokens, completion_tokens, total_tokens, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.project_id, record.title, record.status,
                 record.active_provider, record.active_model, 0, 0, 0,
                 record.created_at, record.updated_at),
            )
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session(row) if row else None

    def list_sessions(self, project_id: str, limit: int = 50, offset: int = 0) -> list[SessionRecord]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions WHERE project_id = ?
                ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
        return [self._session(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        active_provider: str | None = None,
        active_model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> SessionRecord | None:
        current = self.get_session(session_id)
        if current is None:
            return None
        valid_statuses = {"idle", "running", "waiting_approval", "completed", "failed", "cancelled"}
        if status is not None and status not in valid_statuses:
            raise ValueError("会话状态无效")
        clean_title = title.strip() if title is not None else current.title
        if not clean_title or len(clean_title) > 200:
            raise ValueError("会话标题无效")
        record = SessionRecord(
            id=current.id, project_id=current.project_id, title=clean_title,
            status=status or current.status,
            active_provider=active_provider if active_provider is not None else current.active_provider,
            active_model=active_model if active_model is not None else current.active_model,
            prompt_tokens=max(0, prompt_tokens if prompt_tokens is not None else current.prompt_tokens),
            completion_tokens=max(0, completion_tokens if completion_tokens is not None else current.completion_tokens),
            total_tokens=max(0, total_tokens if total_tokens is not None else current.total_tokens),
            created_at=current.created_at, updated_at=_now(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE sessions SET title=?, status=?, active_provider=?, active_model=?,
                prompt_tokens=?, completion_tokens=?, total_tokens=?, updated_at=? WHERE id=?""",
                (record.title, record.status, record.active_provider, record.active_model,
                 record.prompt_tokens, record.completion_tokens, record.total_tokens,
                 record.updated_at, record.id),
            )
        return record

    def delete_session(self, session_id: str) -> bool:
        """删除会话；消息 / 事件 / 幂等表由外键级联清掉。

        连接里已经 `PRAGMA foreign_keys=ON`，而 messages、events、processed_requests
        三张表都是 `REFERENCES sessions(id) ON DELETE CASCADE`，所以删这一行就够了。
        不在这里逐表 DELETE：漏掉一张表就会留下查不到、也删不掉的孤儿数据。

        返回是否真的删到了行（调用方据此区分 404）。**不检查是否在运行**——那是
        app 层的职责（它才看得见内存里的任务表）。
        """
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

    def count_sessions(self, project_id: str) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT count(*) FROM sessions WHERE project_id = ?", (project_id,)).fetchone()[0])

    def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str = "",
        *,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_result: str | None = None,
    ) -> MessageRecord:
        if role not in {"user", "assistant", "tool", "system", "thinking"}:
            raise ValueError("消息角色无效")
        if len(content) > 1_000_000:
            raise ValueError("消息内容过大")
        created_at = _now()
        record = MessageRecord(
            id=uuid4().hex, session_id=session_id, role=role, content=content,
            tool_name=tool_name, tool_args=tool_args, tool_result=tool_result,
            created_at=created_at,
        )
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
                raise KeyError("会话不存在")
            conn.execute(
                """INSERT INTO messages
                (id, session_id, role, content, tool_name, tool_args_json, tool_result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.session_id, record.role, record.content, record.tool_name,
                 json.dumps(record.tool_args, ensure_ascii=False) if record.tool_args is not None else None,
                 record.tool_result, record.created_at),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (created_at, session_id))
        return record

    def list_messages(self, session_id: str, limit: int = 100, offset: int = 0) -> list[MessageRecord]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM messages WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?""",
                (session_id, limit, offset),
            ).fetchall()
        return [self._message(row) for row in rows]

    def list_recent_messages(self, session_id: str, limit: int = 500) -> list[MessageRecord]:
        """最近 limit 条消息，按时间正序返回。

        与 list_messages（正序取最旧 N 条）互补：快照必须"宁可少旧的、不能丢新的"，
        段落落库后消息条数翻数倍，旧窗口会最先截掉最新消息（方案 07 §4.7）。
        """
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM messages WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        records = [self._message(row) for row in rows]
        records.reverse()
        return records

    def list_card_events(
        self, session_id: str, event_types: Sequence[str], limit: int = 1000
    ) -> list[EventRecord]:
        """最近 limit 条指定类型的卡片事件，按 sequence 正序返回。

        按类型过滤后再取"最近 N 条"，回放窗口才不会被高频事件挤占——否则长会话
        重连后丢最新卡片（方案 07 §4.6b）。历史上 message.delta 占事件表的绝大多数
        （每个 token 一行），现已改为**只广播、不落库**（Optimization 01 §5.1）；
        类型过滤仍然保留：老库里还存有大量 delta，且将来任何高频事件都不该挤掉卡片。
        无匹配类型时返回空表。
        """
        types = tuple(dict.fromkeys(str(item) for item in event_types if str(item)))
        if not types:
            return []
        limit = max(1, min(int(limit), 1000))
        placeholders = ",".join("?" for _ in types)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM events WHERE session_id = ? AND event_type IN ({placeholders})
                ORDER BY sequence DESC LIMIT ?""",
                (session_id, *types, limit),
            ).fetchall()
        records = [self._event(row) for row in rows]
        records.reverse()
        return records

    def list_events_by_type(
        self, event_type: str, *, since: str, until: str, limit: int = 5000
    ) -> list[EventRecord]:
        """按事件类型 + 时间窗**跨会话**取事件，按时间正序返回（方案 13 看板用）。

        与 `list_card_events` 的差别：那个按 `session_id` 过滤（重连回放用），这里要跨
        会话聚合。走 `idx_events_type_time(event_type, occurred_at)`；`limit` 卡在 20000，
        调用方拿到"正好等于 limit"的结果时应标 `truncated`，而不是默认它是全量。
        """
        limit = max(1, min(int(limit), 20_000))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM events
                WHERE event_type = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at ASC, rowid ASC LIMIT ?""",
                (str(event_type), str(since), str(until), limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def count_events(self, session_id: str, event_type: str) -> int:
        """某类事件的全量计数（审计展示用，不受回放窗口影响）。"""
        with self._lock, self._connect() as conn:
            return int(conn.execute(
                "SELECT count(*) FROM events WHERE session_id = ? AND event_type = ?",
                (session_id, event_type),
            ).fetchone()[0])

    def count_tool_failures(self, session_id: str) -> int:
        """tool.completed 且 ok=false 的全量计数；json1 不可用时退回 Python 扫描。"""
        row = None
        with self._lock, self._connect() as conn:
            try:
                row = conn.execute(
                    """SELECT count(*) FROM events
                    WHERE session_id = ? AND event_type = 'tool.completed'
                    AND COALESCE(json_extract(data_json, '$.ok'), 1) = 0""",
                    (session_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # json1 不可用，走下面的 Python 回退
        if row is not None:
            return int(row[0])
        # 回退必须在连接块**之外**：共享长连接下，嵌套 `with self._connect()` 会让
        # 内层退出时提前提交外层事务（见 `_connect` 的文档串）。
        records = self.list_card_events(session_id, ("tool.completed",), limit=1000)
        return sum(1 for item in records if not bool(item.data.get("ok", False)))

    def latest_session(self, project_id: str) -> SessionRecord | None:
        records = self.list_sessions(project_id, limit=1)
        return records[0] if records else None

    # ---------- Event log / activity ----------

    @staticmethod
    def _event(row: sqlite3.Row) -> EventRecord:
        try:
            data = json.loads(str(row["data_json"]))
        except (TypeError, json.JSONDecodeError):
            data = {}
        return EventRecord(
            event_id=str(row["event_id"]), sequence=int(row["sequence"]),
            session_id=str(row["session_id"]), project_id=str(row["project_id"]),
            event_type=str(row["event_type"]), data=data if isinstance(data, dict) else {},
            occurred_at=str(row["occurred_at"]),
        )

    def append_event(self, session_id: str, project_id: str, event_type: str, data: dict | None = None) -> EventRecord:
        occurred_at = _now()
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ? AND project_id = ?", (session_id, project_id)).fetchone() is None:
                raise KeyError("会话不存在或不属于该项目")
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0])
            event = EventRecord(uuid4().hex, sequence, session_id, project_id, event_type, data or {}, occurred_at)
            conn.execute(
                "INSERT INTO events (event_id, sequence, session_id, project_id, event_type, data_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.event_id, event.sequence, event.session_id, event.project_id, event.event_type,
                 json.dumps(event.data, ensure_ascii=False), event.occurred_at),
            )
        return event

    def list_events(self, session_id: str, after_sequence: int = 0, limit: int = 500) -> list[EventRecord]:
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?",
                (session_id, max(0, int(after_sequence)), limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def latest_event_sequence(self, session_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?", (session_id,)).fetchone()
        return int(row[0])

    def claim_request(self, session_id: str, request_id: str) -> bool:
        """Atomically claim a client request id for a session."""
        request_id = request_id.strip()
        if not request_id:
            return True
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
                raise KeyError("会话不存在")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO processed_requests (session_id, request_id, created_at) VALUES (?, ?, ?)",
                (session_id, request_id, _now()),
            )
            return cursor.rowcount == 1

    def activity(
        self,
        start: str | None = None,
        end: str | None = None,
        *,
        tz: tzinfo | None = None,
    ) -> list[dict[str, object]]:
        """按**本地日**聚合事件数（热力图数据源）。

        `start` / `end` 是本地日期（YYYY-MM-DD，含两端）。时间窗用 occurred_at 的 UTC
        边界过滤（走索引），分日交给 Python 转本地时区——见文件上方关于时区的说明。
        `tz` 仅供测试注入固定时区。
        """
        local_tz = tz or _local_tz()
        clauses = ["1 = 1"]
        args: list[object] = []
        try:
            if start:
                begin = date.fromisoformat(start)
                clauses.append("occurred_at >= ?")
                args.append(_utc_bounds(begin, begin + timedelta(days=1), local_tz)[0])
            if end:
                finish = date.fromisoformat(end)
                clauses.append("occurred_at < ?")
                args.append(_utc_bounds(finish, finish + timedelta(days=1), local_tz)[1])
        except ValueError as exc:
            raise ValueError(f"日期格式无效：{exc}") from exc
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT occurred_at, project_id FROM events WHERE {' AND '.join(clauses)}",
                args,
            ).fetchall()
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            day = _local_day(str(row["occurred_at"]), local_tz)
            item = grouped.setdefault(day, {"date": day, "count": 0, "projects": {}})
            item["count"] = int(item["count"]) + 1
            projects = item["projects"]
            assert isinstance(projects, dict)
            project_id = str(row["project_id"])
            projects[project_id] = int(projects.get(project_id, 0)) + 1
        return [grouped[day] for day in sorted(grouped)]

    def project_stats(
        self,
        project_id: str,
        *,
        tz: tzinfo | None = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """会话数 / 笔记数 / **今日**调用数。

        "今日"按本地日：原实现用 UTC 日期，东八区在 00:00-08:00 之间会把今天算成
        昨天（与方案 12 的 token 面板同一处修正）。`tz` / `now` 仅供测试注入。
        """
        local_tz = tz or _local_tz()
        today = (now.astimezone(local_tz) if now is not None else datetime.now(local_tz)).date()
        day_start, day_end = _utc_bounds(today, today + timedelta(days=1), local_tz)
        with self._lock, self._connect() as conn:
            sessions = int(conn.execute("SELECT count(*) FROM sessions WHERE project_id = ?", (project_id,)).fetchone()[0])
            notes = int(conn.execute("SELECT count(*) FROM notes WHERE project_id = ?", (project_id,)).fetchone()[0])
            calls = int(conn.execute(
                "SELECT count(*) FROM events WHERE project_id = ? AND event_type = 'tool.started' "
                "AND occurred_at >= ? AND occurred_at < ?",
                (project_id, day_start, day_end),
            ).fetchone()[0])
        return {"sessions": sessions, "notes": notes, "calls_today": calls}

    # ---------- Token 用量（方案 12）----------

    @staticmethod
    def _usage_events_total(conn: sqlite3.Connection) -> int:
        """全量 usage 事件的 total_tokens 之和（只用于与会话累计对账）。

        走新加的 (event_type, occurred_at) 索引，扫的是"每轮一行"而不是整张 events
        表；json1 不可用时（少见）退回 Python 逐行解析，与 count_tool_failures 同一套兜底。
        """
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(json_extract(data_json, '$.total_tokens')), 0) "
                "FROM events WHERE event_type = 'session.usage'"
            ).fetchone()
            return int(row[0] or 0)
        except sqlite3.OperationalError:
            total = 0
            for item in conn.execute(
                "SELECT data_json FROM events WHERE event_type = 'session.usage'"
            ):
                total += _usage_tokens(_usage_payload(str(item["data_json"])))["total"]
            return total

    def usage_summary(
        self,
        *,
        days: int = 30,
        tz: tzinfo | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """首页 token 面板的数据源：今日 / 区间 / 累计 + 按天 + 按项目 + 按档位。

        **两条口径刻意分开返回**（见方案 12 §2）：
        - 按天 / 按项目区间 / 按档位：来自逐轮事件 `session.usage`
        - 累计：来自 `sessions` 的 token 快照求和（会话被删时两边一起减少，所以可比）
        二者差值放在 `drift` 里交给前端判断是否提示——不合并、不猜。

        `tz` / `now` 仅供测试注入固定时区与"现在"，线上走本机时区与当前时间。
        """
        local_tz = tz or _local_tz()
        current = now.astimezone(local_tz) if now is not None else datetime.now(local_tz)
        try:
            span = max(7, min(int(days), 90))  # 越界钳制而不是报错：前端传错不该让首页空掉
        except (TypeError, ValueError):
            span = 30
        today = current.date()
        first_day = today - timedelta(days=span - 1)
        range_start, range_end = _utc_bounds(first_day, today + timedelta(days=1), local_tz)

        def _bucket() -> dict[str, int]:
            return {"prompt": 0, "completion": 0, "total": 0, "turns": 0}

        # 先铺满区间内每一天：折线的 x 轴必须完整，缺日子由前端补 0 容易漏
        daily = {
            (first_day + timedelta(days=offset)).isoformat(): _bucket() for offset in range(span)
        }
        today_bucket = _bucket()
        by_project: dict[str, dict[str, int]] = {}
        by_tier: dict[str, dict[str, int]] = {}

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT occurred_at, project_id, data_json FROM events "
                "WHERE event_type = 'session.usage' AND occurred_at >= ? AND occurred_at < ? "
                "ORDER BY occurred_at ASC",
                (range_start, range_end),
            ).fetchall()
            lifetime_rows = conn.execute(
                "SELECT project_id, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(completion_tokens), 0) AS completion, "
                "COALESCE(SUM(total_tokens), 0) AS total, "
                "COUNT(*) AS sessions "
                "FROM sessions GROUP BY project_id"
            ).fetchall()
            events_total = self._usage_events_total(conn)

        today_key = today.isoformat()
        for row in rows:
            payload = _usage_payload(str(row["data_json"]))
            tokens = _usage_tokens(payload)
            day = _local_day(str(row["occurred_at"]), local_tz)
            bucket = daily.get(day)
            if bucket is not None:
                _accumulate(bucket, tokens)
            if day == today_key:
                _accumulate(today_bucket, tokens)
            project_id = str(row["project_id"])
            project_bucket = by_project.setdefault(project_id, {"today": 0, "range": 0, "lifetime": 0, "turns": 0})
            project_bucket["range"] += tokens["total"]
            project_bucket["turns"] += 1
            if day == today_key:
                project_bucket["today"] += tokens["total"]
            # 老事件没有档位字段（方案 12 §4.2 才补上），归入「未标注」而不是猜
            tier = str(payload.get("tier") or "") or "未标注"
            tier_bucket = by_tier.setdefault(tier, {"total": 0, "turns": 0})
            tier_bucket["total"] += tokens["total"]
            tier_bucket["turns"] += 1

        lifetime = {"prompt": 0, "completion": 0, "total": 0, "sessions": 0}
        for row in lifetime_rows:
            project_id = str(row["project_id"])
            # 没有区间用量的项目也要出现在这里：项目卡要显示「累计」那一列
            project_bucket = by_project.setdefault(project_id, {"today": 0, "range": 0, "lifetime": 0, "turns": 0})
            project_bucket["lifetime"] = int(row["total"] or 0)
            lifetime["prompt"] += int(row["prompt"] or 0)
            lifetime["completion"] += int(row["completion"] or 0)
            lifetime["total"] += int(row["total"] or 0)
            lifetime["sessions"] += int(row["sessions"] or 0)

        range_bucket = _bucket()
        for bucket in daily.values():
            range_bucket["prompt"] += bucket["prompt"]
            range_bucket["completion"] += bucket["completion"]
            range_bucket["total"] += bucket["total"]
            range_bucket["turns"] += bucket["turns"]

        sessions_total = int(lifetime["total"])
        diff = int(events_total) - sessions_total
        return {
            "timezone": str(local_tz),
            "generated_at": current.isoformat(timespec="seconds"),
            "days": span,
            "today": today_bucket,
            "range": range_bucket,
            "lifetime": lifetime,
            "daily": [{"date": day, **bucket} for day, bucket in sorted(daily.items())],
            "by_project": [
                {"project_id": project_id, **bucket} for project_id, bucket in sorted(by_project.items())
            ],
            "by_tier": [
                {"tier": tier, **bucket}
                for tier, bucket in sorted(by_tier.items(), key=lambda item: -item[1]["total"])
            ],
            "drift": {
                "events_total": int(events_total),
                "sessions_total": sessions_total,
                "diff": diff,
                "diff_pct": round(abs(diff) / sessions_total * 100.0, 2) if sessions_total else 0.0,
            },
        }

    # ---------- Notes ----------

    @staticmethod
    def _clean_note(title: str, body_markdown: str, tags: list[str] | tuple[str, ...]) -> tuple[str, str, tuple[str, ...]]:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("笔记标题不能为空")
        if len(clean_title) > 200:
            raise ValueError("笔记标题不能超过 200 个字符")
        if len(body_markdown) > 2_000_000:
            raise ValueError("笔记内容过大")
        clean_tags = tuple(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
        if len(clean_tags) > 50 or any(len(tag) > 50 for tag in clean_tags):
            raise ValueError("笔记标签数量或长度超限")
        return clean_title, body_markdown, clean_tags

    def create_note(
        self,
        title: str,
        body_markdown: str = "",
        tags: list[str] | tuple[str, ...] = (),
        project_id: str | None = None,
    ) -> NoteRecord:
        clean_title, body_markdown, clean_tags = self._clean_note(title, body_markdown, tags)
        timestamp = _now()
        record = NoteRecord(
            id=uuid4().hex, project_id=project_id, title=clean_title,
            body_markdown=body_markdown, tags=clean_tags, pinned=False,
            created_at=timestamp, updated_at=timestamp, version=1,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO notes
                (id, project_id, title, body_markdown, tags_json, pinned, created_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, 1)""",
                (record.id, record.project_id, record.title, record.body_markdown,
                 json.dumps(record.tags, ensure_ascii=False), timestamp, timestamp),
            )
        return record

    def get_note(self, note_id: str) -> NoteRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return self._note(row) if row else None

    def list_notes(
        self,
        *,
        scope: Literal["global", "project", "all"],
        project_id: str | None = None,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[NoteRecord]:
        if scope == "project" and not project_id:
            raise ValueError("项目范围查询缺少 project_id")
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        clauses: list[str] = []
        args: list[object] = []
        if scope == "global" or scope == "all":
            # The global navigation is the workspace-wide view: it includes
            # global notes and notes associated with every project.
            pass
        elif scope == "project":
            # 项目范围查询**只**返回挂在这个项目下的笔记：全局笔记（project_id 为空）
            # 不属于任何项目，也不进任何项目的会话上下文。
            clauses.append("project_id = ?")
            args.append(project_id)
        clean_query = query.strip().casefold()
        if clean_query:
            clauses.append("(instr(lower(title), ?) > 0 OR instr(lower(body_markdown), ?) > 0 OR instr(lower(tags_json), ?) > 0)")
            args.extend([clean_query, clean_query, clean_query])
        where = " AND ".join(clauses) or "1 = 1"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notes WHERE {where} ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [self._note(row) for row in rows]

    def update_note(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body_markdown: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        project_id: str | None | object = _MISSING,
        expected_version: int | None = None,
    ) -> NoteRecord | None:
        current = self.get_note(note_id)
        if current is None:
            return None
        clean_title, clean_body, clean_tags = self._clean_note(
            title if title is not None else current.title,
            body_markdown if body_markdown is not None else current.body_markdown,
            tags if tags is not None else current.tags,
        )
        new_project_id = current.project_id if project_id is _MISSING else project_id
        timestamp = _now()
        new_version = current.version + 1
        with self._lock, self._connect() as conn:
            sql = """UPDATE notes SET title=?, body_markdown=?, tags_json=?, project_id=?, updated_at=?, version=?
                     WHERE id=?"""
            params: list[object] = [clean_title, clean_body, json.dumps(clean_tags, ensure_ascii=False),
                                    new_project_id, timestamp, new_version, note_id]
            if expected_version is not None:
                sql += " AND version=?"
                params.append(expected_version)
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                if expected_version is not None:
                    raise NoteConflictError("笔记已被其他位置更新")
                return None
        return self.get_note(note_id)

    def set_note_pinned(self, note_id: str, pinned: bool | None = None) -> NoteRecord | None:
        current = self.get_note(note_id)
        if current is None:
            return None
        value = not current.pinned if pinned is None else pinned
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE notes SET pinned=?, updated_at=?, version=version+1 WHERE id=?",
                (int(value), _now(), note_id),
            )
        return self.get_note(note_id)

    def delete_note(self, note_id: str, *, expected_version: int | None = None) -> bool:
        """删除一条笔记。`expected_version` 给定时做成原子操作（版本不符则不删）。

        与 `update_note` 的差别：这里返回 bool 而不是抛 `NoteConflictError`，因为
        "删 0 行"同时可能是"笔记已不在"；调用方需要区分时再查一次（见
        `routivus/tool/notes.py` 的失败路径）。不传版本时行为与旧版一致。
        """
        with self._lock, self._connect() as conn:
            sql = "DELETE FROM notes WHERE id = ?"
            params: list[object] = [note_id]
            if expected_version is not None:
                sql += " AND version = ?"
                params.append(expected_version)
            return conn.execute(sql, params).rowcount > 0

    def count_notes(self, project_id: str | None = None) -> int:
        clause = "project_id IS NULL" if project_id is None else "project_id = ?"
        args = () if project_id is None else (project_id,)
        with self._lock, self._connect() as conn:
            return int(conn.execute(f"SELECT count(*) FROM notes WHERE {clause}", args).fetchone()[0])

    def note_stats(self) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            total = int(conn.execute("SELECT count(*) FROM notes").fetchone()[0])
            global_notes = int(conn.execute("SELECT count(*) FROM notes WHERE project_id IS NULL").fetchone()[0])
            rows = conn.execute(
                "SELECT project_id, count(*) AS count FROM notes WHERE project_id IS NOT NULL GROUP BY project_id"
            ).fetchall()
        by_project = {str(row["project_id"]): int(row["count"]) for row in rows}
        return {
            "total": total,
            "global_notes": global_notes,
            "project_notes": sum(by_project.values()),
            "by_project": by_project,
        }
