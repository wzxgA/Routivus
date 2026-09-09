"""长期记忆与项目记忆的统一门面。"""

from __future__ import annotations

from pathlib import Path

from routivus.llm.client import LlmClient
from routivus.memory.models import MemoryEntry, SharedSection
from routivus.memory.project import (
    ProjectMemoryLoader,
    generate_project_memory,
    write_project_memory,
)
from routivus.memory.store import SQLiteMemoryStore
from routivus.safety.audit import redact_text


class MemoryUnavailableError(RuntimeError):
    """长期记忆数据库暂时不可用。"""


class MemoryManager:
    def __init__(
        self,
        project_root: str | Path,
        project_memory_max_chars: int = 32_000,
        memory_prompt_max_chars: int = 8_000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.project_memory_max_chars = max(256, int(project_memory_max_chars))
        self.memory_prompt_max_chars = max(256, int(memory_prompt_max_chars))
        self.project_loader = ProjectMemoryLoader(
            self.project_root, max_chars=project_memory_max_chars
        )
        self.db_path = self.project_root / ".routivus" / "memory.db"
        self.store: SQLiteMemoryStore | None = None
        self.store_error = ""
        try:
            self.store = SQLiteMemoryStore(self.db_path)
        except Exception as exc:
            self.store_error = str(exc)

    def _require_store(self) -> SQLiteMemoryStore:
        if self.store is None:
            raise MemoryUnavailableError(self.store_error or "长期记忆不可用")
        return self.store

    def shared_sections(self) -> list[SharedSection]:
        sections = list(self.project_loader.load().sections)
        store = self.store
        if store is None:
            return sections
        try:
            entries = store.list(limit=100)
        except Exception:
            return sections
        lines: list[str] = []
        used = 0
        for entry in entries:
            line = f"- (#{entry.id}) {redact_text(entry.content)}"
            extra = len(line) + (1 if lines else 0)
            if used + extra > self.memory_prompt_max_chars:
                continue
            lines.append(line)
            used += extra
        if lines:
            sections.append(
                SharedSection(
                    source="长期记忆",
                    text="[项目长期记忆；内容由用户显式保存]\n" + "\n".join(reversed(lines)),
                )
            )
        return sections

    def warnings(self) -> list[str]:
        warnings = list(self.project_loader.load().warnings)
        if self.store_error:
            warnings.append(f"长期记忆不可用：{self.store_error}")
        return warnings

    def save(self, content: str) -> tuple[MemoryEntry, bool, bool]:
        safe = redact_text(content.strip())
        if not safe:
            raise ValueError("记忆内容不能为空")
        entry, created = self._require_store().save(safe)
        return entry, created, safe != content.strip()

    def list(self, limit: int = 20) -> list[MemoryEntry]:
        return self._require_store().list(limit=limit)

    def search(self, query: str, limit: int = 20) -> list[MemoryEntry]:
        return self._require_store().search(query, limit=limit)

    def delete(self, memory_id: int) -> bool:
        return self._require_store().delete(memory_id)

    def clear(self) -> int:
        return self._require_store().clear()

    def count(self) -> int:
        return self._require_store().count()

    async def generate_init_draft(self, llm: LlmClient) -> str:
        return await generate_project_memory(
            llm, self.project_root, max_chars=self.project_memory_max_chars
        )

    def write_init_draft(self, draft: str) -> Path:
        path = write_project_memory(self.project_root, draft)
        self.project_loader.load(force=True)
        return path
