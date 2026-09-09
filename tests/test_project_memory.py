"""Routivus.md / Routivus.local.md 测试。"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from routivus.llm.client import LlmClient
from routivus.llm.types import StreamEvent
from routivus.memory.manager import MemoryManager
from routivus.memory.project import ProjectMemoryLoader


class DraftClient(LlmClient):
    async def stream_chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(kind="content", text="## 技术栈\nPython 3.11")
        yield StreamEvent(kind="done")


def test_project_memory_loads_ordered_and_redacts(tmp_path):
    (tmp_path / "Routivus.md").write_text("共享约定\napi_key=secret-value\n", encoding="utf-8")
    (tmp_path / "Routivus.local.md").write_text("本地偏好\n", encoding="utf-8")
    loader = ProjectMemoryLoader(tmp_path)

    snapshot = loader.load()
    assert [item.source for item in snapshot.sections] == ["Routivus.md", "Routivus.local.md"]
    assert "secret-value" not in snapshot.sections[0].text
    assert "api_key=***" in snapshot.sections[0].text

    (tmp_path / "Routivus.local.md").write_text("更新后的偏好\n", encoding="utf-8")
    refreshed = loader.load()
    assert "更新后的偏好" in refreshed.sections[1].text


@pytest.mark.asyncio
async def test_init_generates_and_does_not_overwrite(tmp_path):
    manager = MemoryManager(tmp_path)
    draft = await manager.generate_init_draft(DraftClient())
    path = manager.write_init_draft(draft)
    assert path.name == "Routivus.md"
    assert "技术栈" in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        await manager.generate_init_draft(DraftClient())
