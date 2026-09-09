"""SQLite 长期记忆测试。"""

from __future__ import annotations

from routivus.memory.store import SQLiteMemoryStore


def test_store_crud_duplicate_and_unicode_search(tmp_path):
    store = SQLiteMemoryStore(tmp_path / ".routivus" / "memory.db")

    first, created = store.save("  测试命令：uv run pytest  ")
    assert created
    duplicate, created_again = store.save("测试命令：uv   run pytest")
    assert not created_again
    assert duplicate.id == first.id

    second, created = store.save("API 返回字段使用 snake_case")
    assert created
    assert [item.id for item in store.search("PYTEST")] == [first.id]
    assert [item.id for item in store.search("snake_case")] == [second.id]
    assert store.delete(first.id)
    assert not store.delete(first.id)
    assert store.count() == 1
    assert store.clear() == 1
    assert store.count() == 0


def test_store_is_project_scoped_by_database_path(tmp_path):
    one = SQLiteMemoryStore(tmp_path / "one" / ".routivus" / "memory.db")
    two = SQLiteMemoryStore(tmp_path / "two" / ".routivus" / "memory.db")
    one.save("只属于项目 one")

    assert len(one.list()) == 1
    assert two.list() == []
