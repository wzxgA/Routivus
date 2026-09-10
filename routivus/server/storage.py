"""SQLite persistence for Web Console sessions, messages and notes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4


SessionStatus = Literal["idle", "running", "waiting_approval", "completed", "failed", "cancelled"]
MessageRole = Literal["user", "assistant", "tool", "system"]
_MISSING = object()


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


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WorkspaceStore:
    """Thread-safe SQLite store for server-owned workspace data."""

    VERSION = 2

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
            status=status or current.status, active_provider=current.active_provider,
            active_model=current.active_model,
            prompt_tokens=max(0, prompt_tokens if prompt_tokens is not None else current.prompt_tokens),
            completion_tokens=max(0, completion_tokens if completion_tokens is not None else current.completion_tokens),
            total_tokens=max(0, total_tokens if total_tokens is not None else current.total_tokens),
            created_at=current.created_at, updated_at=_now(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE sessions SET title=?, status=?, prompt_tokens=?, completion_tokens=?,
                total_tokens=?, updated_at=? WHERE id=?""",
                (record.title, record.status, record.prompt_tokens, record.completion_tokens,
                 record.total_tokens, record.updated_at, record.id),
            )
        return record

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
        if role not in {"user", "assistant", "tool", "system"}:
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

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

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

    def activity(self, start: str | None = None, end: str | None = None) -> list[dict[str, object]]:
        clauses = ["1 = 1"]
        args: list[object] = []
        if start:
            clauses.append("substr(occurred_at, 1, 10) >= ?")
            args.append(start)
        if end:
            clauses.append("substr(occurred_at, 1, 10) <= ?")
            args.append(end)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT substr(occurred_at, 1, 10) AS day, project_id, count(*) AS count FROM events WHERE {' AND '.join(clauses)} GROUP BY day, project_id ORDER BY day ASC",
                args,
            ).fetchall()
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            item = grouped.setdefault(str(row["day"]), {"date": str(row["day"]), "count": 0, "projects": {}})
            item["count"] = int(item["count"]) + int(row["count"])
            projects = item["projects"]
            assert isinstance(projects, dict)
            projects[str(row["project_id"])] = int(row["count"])
        return list(grouped.values())

    def project_stats(self, project_id: str) -> dict[str, int]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock, self._connect() as conn:
            sessions = int(conn.execute("SELECT count(*) FROM sessions WHERE project_id = ?", (project_id,)).fetchone()[0])
            notes = int(conn.execute("SELECT count(*) FROM notes WHERE project_id = ?", (project_id,)).fetchone()[0])
            calls = int(conn.execute(
                "SELECT count(*) FROM events WHERE project_id = ? AND event_type = 'tool.started' AND substr(occurred_at, 1, 10) = ?",
                (project_id, today),
            ).fetchone()[0])
        return {"sessions": sessions, "notes": notes, "calls_today": calls}

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
                    raise ValueError("笔记已被其他位置更新")
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

    def delete_note(self, note_id: str) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0

    def count_notes(self, project_id: str | None = None) -> int:
        clause = "project_id IS NULL" if project_id is None else "project_id = ?"
        args = () if project_id is None else (project_id,)
        with self._lock, self._connect() as conn:
            return int(conn.execute(f"SELECT count(*) FROM notes WHERE {clause}", args).fetchone()[0])
