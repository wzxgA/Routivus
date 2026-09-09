"""In-memory history, cursor navigation and optional persistence."""

from __future__ import annotations

from pathlib import Path

from routivus.input_history.models import HistoryConfig, HistoryCursor, HistoryEntry
from routivus.input_history.persistence import HistoryPersistence, history_path, now_iso, project_scope
from routivus.input_history.policy import is_sensitive, normalize_text, should_record


class InputHistory:
    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        user_dir: str | Path | None = None,
        config: HistoryConfig | None = None,
        persistence: HistoryPersistence | None = None,
    ) -> None:
        self.config = config or HistoryConfig()
        self.scope = project_scope(project_root)
        self.persistence = persistence
        if self.persistence is None and user_dir is not None and self.config.persist:
            self.persistence = HistoryPersistence(
                history_path(user_dir, self.scope), self.scope, self.config
            )
        self._entries: list[HistoryEntry] = []
        self._cursor = HistoryCursor()
        if self.config.enabled and self.config.persist and self.persistence is not None:
            self._entries = self._dedupe(self.persistence.load())[-self.config.max_entries:]

    @property
    def cursor(self) -> HistoryCursor:
        return self._cursor

    def add(self, text: str) -> bool:
        value = normalize_text(text)
        if not self.config.enabled or not should_record(value):
            return False
        if len(value) > self.config.max_entry_chars:
            return False
        if self._entries and self._entries[-1].text == value:
            self.reset_cursor()
            return False
        was_full = len(self._entries) >= self.config.max_entries
        entry = HistoryEntry(value, now_iso(), self.scope, persisted=False)
        self._entries.append(entry)
        self._entries = self._dedupe(self._entries)[-self.config.max_entries:]
        if self.config.persist and self.persistence is not None and not is_sensitive(value):
            try:
                saved = (
                    self.persistence.rewrite(self._entries)
                    if was_full
                    else self.persistence.append(entry, self._entries)
                )
            except OSError:
                saved = False
            if saved:
                self._entries[-1] = HistoryEntry(
                    entry.text, entry.created_at, entry.scope, persisted=True
                )
        self.reset_cursor()
        return True

    def previous(self, draft: str = "") -> str:
        if not self.config.enabled or not self._entries:
            return draft
        if self._cursor.index is None:
            self._cursor.draft = draft
            self._cursor.index = len(self._entries) - 1
        else:
            self._cursor.index = max(0, self._cursor.index - 1)
        return self._entries[self._cursor.index].text

    def next(self) -> str:
        if not self.config.enabled or self._cursor.index is None:
            return self._cursor.draft if self._cursor.index is None else ""
        if self._cursor.index >= len(self._entries) - 1:
            draft = self._cursor.draft
            # Keep the draft while sitting at the end.  A repeated Down key
            # should remain stable instead of replacing it with an empty
            # value; the next Up call will capture the current Composer value
            # again if the user starts a new navigation pass.
            self._cursor.index = None
            return draft
        self._cursor.index += 1
        return self._entries[self._cursor.index].text

    def reset_cursor(self) -> None:
        self._cursor = HistoryCursor()

    def clear(self, *, persistent: bool = True) -> int:
        count = len(self._entries)
        self._entries.clear()
        self.reset_cursor()
        if persistent and self.persistence is not None:
            self.persistence.clear()
        return count

    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    def status(self) -> str:
        if not self.config.enabled:
            return "输入历史：关闭"
        persistence = "开启" if self.config.persist and self.persistence is not None else "关闭"
        return f"输入历史：开启\n当前项目记录：{len(self._entries)} 条\n持久化：{persistence}"

    @staticmethod
    def _dedupe(entries: list[HistoryEntry]) -> list[HistoryEntry]:
        result: list[HistoryEntry] = []
        for entry in entries:
            if result and result[-1].text == entry.text:
                result[-1] = entry
            else:
                result.append(entry)
        return result
