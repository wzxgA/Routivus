"""Small, bounded JSONL persistence for input history."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from routivus.input_history.models import HistoryConfig, HistoryEntry


def project_scope(project_root: str | Path | None) -> str:
    if project_root is None:
        return "global"
    resolved = str(Path(project_root).resolve()).casefold()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def history_path(user_dir: str | Path, scope: str) -> Path:
    return Path(user_dir).resolve() / "input-history" / f"{scope}.jsonl"


class HistoryPersistence:
    def __init__(self, path: str | Path, scope: str, config: HistoryConfig) -> None:
        self.path = Path(path)
        self.scope = scope
        self.config = config

    def load(self) -> list[HistoryEntry]:
        try:
            if not self.path.is_file():
                return []
            data = self.path.read_bytes()
        except OSError:
            return []
        if len(data) > self.config.max_file_bytes:
            data = data[-self.config.max_file_bytes:]
        entries: list[HistoryEntry] = []
        for raw_line in data.splitlines():
            try:
                item = json.loads(raw_line.decode("utf-8"))
                text = item.get("text")
                created_at = item.get("created_at")
                if (
                    not isinstance(text, str)
                    or not isinstance(created_at, str)
                    or not text.strip()
                    or len(text) > self.config.max_entry_chars
                    or item.get("scope") != self.scope
                ):
                    continue
                entries.append(HistoryEntry(text, created_at, self.scope, persisted=True))
            except (UnicodeError, AttributeError, TypeError, ValueError):
                continue
        return entries[-self.config.max_entries:]

    def append(self, entry: HistoryEntry, entries: list[HistoryEntry]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._restrict_permissions(self.path.parent, directory=True)
            line = self._encode(entry)
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size + len(line) <= self.config.max_file_bytes:
                with self.path.open("ab") as handle:
                    handle.write(line)
                self._restrict_permissions(self.path)
                return True
            self.rewrite(entries)
            return True
        except OSError:
            return False

    def clear(self) -> bool:
        try:
            if self.path.exists():
                self.path.unlink()
            return True
        except OSError:
            return False

    def rewrite(self, entries: list[HistoryEntry]) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(self.path.parent, directory=True)
        fd, raw_path = tempfile.mkstemp(prefix=".history-", suffix=".tmp", dir=self.path.parent)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as handle:
                for entry in entries[-self.config.max_entries:]:
                    line = self._encode(entry)
                    if handle.tell() + len(line) > self.config.max_file_bytes:
                        continue
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.path)
            self._restrict_permissions(self.path)
            return True
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _encode(entry: HistoryEntry) -> bytes:
        return (
            json.dumps(
                {"text": entry.text, "created_at": entry.created_at, "scope": entry.scope},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _restrict_permissions(path: Path, *, directory: bool = False) -> None:
        if os.name != "nt":
            try:
                path.chmod(0o700 if directory else 0o600)
            except OSError:
                pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
