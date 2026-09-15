"""把 `WorkspaceStore` 适配成工具层要的 `NotesSource`（见 plans/tools/notes-write-tools.md §3.2）。

只做两件事：转发方法、把异常翻译成工具层的词汇。**不搬任何业务判断** —— 归属校验
（"这条笔记属不属于当前项目"）仍在 `routivus/tool/notes.py` 里，与已上线的读工具一致。

为什么需要这层：`NoteConflictError` 定义在 `routivus/server`（上层），而 `tool/` 是下层，
工具层不该 import 它。翻译放在这里，两边各自只认识自己的词汇。
"""

from __future__ import annotations

from typing import Sequence

from routivus.server.storage import NoteConflictError, WorkspaceStore
from routivus.tool.notes import NoteConflict, NotesWriteError


class StoreNotesSource:
    """`WorkspaceStore` → `NotesSource`。读方法原样透传，写方法翻译异常。"""

    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    # ---------- 读：原样透传 ----------

    def list_notes(
        self,
        *,
        scope: str,
        project_id: str | None = None,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ):
        return self._store.list_notes(
            scope=scope,  # type: ignore[arg-type]
            project_id=project_id,
            query=query,
            limit=limit,
            offset=offset,
        )

    def get_note(self, note_id: str):
        return self._store.get_note(note_id)

    # ---------- 写：翻译异常 ----------

    def create_note(
        self,
        *,
        title: str,
        body_markdown: str,
        tags: Sequence[str],
        project_id: str,
    ):
        try:
            return self._store.create_note(title, body_markdown, tags, project_id)
        except ValueError as exc:
            raise self._translate(exc) from exc

    def update_note(
        self,
        note_id: str,
        *,
        title: str | None,
        body_markdown: str | None,
        tags: Sequence[str] | None,
        expected_version: int,
    ):
        try:
            return self._store.update_note(
                note_id,
                title=title,
                body_markdown=body_markdown,
                tags=tags,
                expected_version=expected_version,
            )
        except ValueError as exc:
            raise self._translate(exc) from exc

    def delete_note(self, note_id: str, *, expected_version: int) -> bool:
        """原子删除。返回 False 表示没删到（笔记已不在，或版本不符）。

        与 `update_note` 不同，这里**不抛冲突**：删 0 行时"已不存在"与"版本不符"要给出
        不同指引，而 store 的单条 DELETE 区分不了。工具层在失败路径上再查一次决定文案。
        """
        return self._store.delete_note(note_id, expected_version=expected_version)

    @staticmethod
    def _translate(exc: ValueError) -> NotesWriteError:
        """`NoteConflictError` 必须先于 `ValueError` 判断 —— 它是 `ValueError` 的子类。

        顺序写反的后果不是崩，而是把"别人改过了，先重读"错报成"标题不能为空"这类校验
        错误，模型收到的指引完全相反。`tests/test_notes_write_tools.py` 专门钉这一条。
        """
        if isinstance(exc, NoteConflictError):
            return NoteConflict(str(exc))
        return NotesWriteError(str(exc))
