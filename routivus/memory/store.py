"""项目级 SQLite 长期记忆仓库。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from routivus.memory.models import MemoryEntry


SCHEMA_VERSION = 1


def normalize_content(content: str) -> str:
    return " ".join(content.split()).casefold()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                        normalized_content TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'manual',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_normalized
                        ON memories(normalized_content);
                    CREATE INDEX IF NOT EXISTS idx_memories_updated
                        ON memories(updated_at DESC, id DESC);
                    PRAGMA user_version = 1;
                    """
                )

    @staticmethod
    def _entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=int(row["id"]),
            content=str(row["content"]),
            source=str(row["source"]),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    def save(self, content: str, source: str = "manual") -> tuple[MemoryEntry, bool]:
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if len(content) > 4000:
            raise ValueError("单条记忆最多 4000 个字符")
        normalized = normalize_content(content)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE normalized_content = ?", (normalized,)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE memories SET updated_at = ?, source = ? WHERE id = ?",
                    (now, source, int(row["id"])),
                )
                row = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (int(row["id"]),)
                ).fetchone()
                assert row is not None
                return self._entry(row), False
            cursor = conn.execute(
                """
                INSERT INTO memories(content, normalized_content, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (content, normalized, source, now, now),
            )
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            assert row is not None
            return self._entry(row), True

    def list(self, limit: int = 20, offset: int = 0) -> list[MemoryEntry]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def search(self, query: str, limit: int = 20) -> list[MemoryEntry]:
        query = normalize_content(query)
        if not query:
            return []
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE instr(normalized_content, ?) > 0
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def delete(self, memory_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            return cursor.rowcount > 0

    def clear(self) -> int:
        with self._connect() as conn:
            count = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
            conn.execute("DELETE FROM memories")
            return count

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
