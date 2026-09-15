"""笔记写工具（notes_create / notes_update / notes_delete）的测试。

设计与取舍见 plans/tools/notes-write-tools.md。这里守的是：

1. **写权限窄于读权限**：只能写当前项目的笔记，全局笔记与别的项目一律"不存在"；
2. **不许盲改**：改与删必须带 `expected_version`，删还要回显标题；
3. **冲突不自动重试**：失败并让模型重读，且错误里**不给**当前版本号；
4. **审计不留正文**：写工具的 args 里正文被替换成 `[N 字已省略]`；
5. 适配器的异常翻译方向正确（`NoteConflictError` 不能被当成校验错误）。
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from routivus.config.settings import Settings, load_settings
from routivus.safety.audit import AuditLogger
from routivus.safety.hitl import HITLPolicy
from routivus.server import ProjectRegistry, create_app
from routivus.server.app import _build_default_agent
from routivus.server.config import ServerConfig
from routivus.server.notes_source import StoreNotesSource
from routivus.server.projects import ProjectRecord
from routivus.server.storage import NoteConflictError, WorkspaceStore
from routivus.tool.builtin import build_registry
from routivus.tool.notes import (
    MAX_WRITE_CHARS,
    NoteConflict,
    NotesWriteError,
)

PROJECT_A = "p-alpha"
PROJECT_B = "p-beta"
WRITE_TOOLS = ("notes_create", "notes_update", "notes_delete")
NOT_FOUND_PREFIX = "笔记不存在: "
VERSION_RE = re.compile(r"version=(\d+)")


def _store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "workspace.sqlite3")


def _source(store: WorkspaceStore) -> StoreNotesSource:
    return StoreNotesSource(store)


def _registry(store: WorkspaceStore, tmp_path: Path, project_id: str = PROJECT_A, **kwargs):
    return build_registry(
        base_dir=tmp_path, notes_source=_source(store), project_id=project_id, **kwargs
    )


def _note(store: WorkspaceStore, title: str, body: str = "正文", tags=("ops",), project_id=PROJECT_A):
    return store.create_note(title, body, list(tags), project_id)


def _version(registry, note_id: str) -> int:
    """模拟"模型先读过"：从 notes_read 的输出里取版本号。"""
    result = registry.execute("notes_read", {"note_id": note_id})
    assert result.ok
    return int(VERSION_RE.search(result.output).group(1))


# ---------- 注册与开关 ----------


def test_write_tools_not_registered_without_source(tmp_path):
    names = build_registry(base_dir=tmp_path).names()
    for tool in WRITE_TOOLS:
        assert tool not in names


def test_read_tools_survive_write_toggle(tmp_path):
    registry = _registry(_store(tmp_path), tmp_path, notes_write_enabled=False)
    assert {"notes_list", "notes_read"} <= set(registry.names())
    for tool in WRITE_TOOLS:
        assert tool not in registry.names()


def test_write_tools_registered_by_default(tmp_path):
    registry = _registry(_store(tmp_path), tmp_path)
    for tool in WRITE_TOOLS:
        assert registry.get(tool).source == "builtin-notes"


def test_create_schema_has_no_project_field(tmp_path):
    """机械保证"模型无法指定别的项目"：schema 里根本没有这个字段。"""
    registry = _registry(_store(tmp_path), tmp_path)
    for tool in WRITE_TOOLS:
        properties = registry.get(tool).parameters["properties"]
        assert "project_id" not in properties


def test_write_schemas_require_version(tmp_path):
    registry = _registry(_store(tmp_path), tmp_path)
    assert "expected_version" in registry.get("notes_update").parameters["required"]
    assert set(registry.get("notes_delete").parameters["required"]) == {
        "note_id",
        "expected_title",
        "expected_version",
    }


# ---------- notes_create ----------


def test_create_lands_in_current_project(tmp_path):
    store = _store(tmp_path)
    result = _registry(store, tmp_path).execute(
        "notes_create", {"title": "上线检查表", "body_markdown": "回滚步骤", "tags": ["ops", "deploy"]}
    )

    assert result.ok
    assert "已创建笔记" in result.output and "version=1" in result.output
    created = store.list_notes(scope="project", project_id=PROJECT_A)
    assert [(item.title, item.body_markdown, list(item.tags)) for item in created] == [
        ("上线检查表", "回滚步骤", ["ops", "deploy"])
    ]


def test_create_rejects_bad_title(tmp_path):
    registry = _registry(_store(tmp_path), tmp_path)
    assert not registry.execute("notes_create", {"title": "   "}).ok
    assert not registry.execute("notes_create", {"title": "x" * 201}).ok


def test_create_rejects_oversized_body_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    result = _registry(store, tmp_path).execute("notes_create", {"title": "长", "body_markdown": "x" * (MAX_WRITE_CHARS + 1)})

    assert not result.ok
    assert "超过上限" in result.error
    assert store.list_notes(scope="project", project_id=PROJECT_A) == []


def test_create_rejects_bad_tags(tmp_path):
    registry = _registry(_store(tmp_path), tmp_path)
    assert not registry.execute("notes_create", {"title": "t", "tags": ["x" * 33]}).ok
    assert not registry.execute("notes_create", {"title": "t", "tags": [f"tag{i}" for i in range(11)]}).ok


def test_create_warns_about_same_title(tmp_path):
    store = _store(tmp_path)
    _note(store, "上线检查表")
    result = _registry(store, tmp_path).execute("notes_create", {"title": "上线检查表", "body_markdown": "另一份"})

    assert result.ok  # 同名只是提示，不是拦截
    assert "已存在同名笔记" in result.output
    assert "notes_update" in result.output


# ---------- notes_update ----------


def test_update_body_bumps_version_and_lists_changes(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "上线检查表", "旧正文")
    registry = _registry(store, tmp_path)

    result = registry.execute(
        "notes_update", {"note_id": note.id, "expected_version": _version(registry, note.id), "body_markdown": "新正文"}
    )

    assert result.ok
    assert "version 1 → 2" in result.output and "改动：正文" in result.output
    assert store.get_note(note.id).body_markdown == "新正文"


def test_update_single_field_title_or_tags(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "旧标题", "正文", ("ops",))
    registry = _registry(store, tmp_path)

    renamed = registry.execute(
        "notes_update", {"note_id": note.id, "expected_version": 1, "title": "新标题"}
    )
    assert renamed.ok and "改动：标题" in renamed.output
    assert store.get_note(note.id).body_markdown == "正文"  # 没动的字段保持原样

    retagged = registry.execute(
        "notes_update", {"note_id": note.id, "expected_version": 2, "tags": ["a", "b"]}
    )
    assert retagged.ok and list(store.get_note(note.id).tags) == ["a", "b"]


def test_update_append_keeps_existing_body(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "排障记录", "第一步：看日志")
    registry = _registry(store, tmp_path)

    result = registry.execute(
        "notes_update",
        {"note_id": note.id, "expected_version": 1, "body_markdown": "第二步：回滚", "append": True},
    )

    assert result.ok and "正文追加" in result.output
    assert store.get_note(note.id).body_markdown == "第一步：看日志\n第二步：回滚"


def test_update_append_rejects_other_fields(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "排障记录")
    registry = _registry(store, tmp_path)

    result = registry.execute(
        "notes_update",
        {"note_id": note.id, "expected_version": 1, "body_markdown": "x", "append": True, "title": "改名"},
    )

    assert not result.ok and "append 模式只能给 body_markdown" in result.error


def test_update_requires_at_least_one_field(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "笔记")
    result = _registry(store, tmp_path).execute(
        "notes_update", {"note_id": note.id, "expected_version": 1}
    )
    assert not result.ok and "没有要改的字段" in result.error


def test_update_conflict_asks_for_reread_and_keeps_content(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "上线检查表", "agent 读到的版本")
    registry = _registry(store, tmp_path)
    stale = _version(registry, note.id)

    store.update_note(note.id, body_markdown="别人改过的版本")  # 模拟并发编辑

    result = registry.execute(
        "notes_update", {"note_id": note.id, "expected_version": stale, "body_markdown": "agent 想写的"}
    )

    assert not result.ok
    assert "已被其他位置更新" in result.error and "notes_read" in result.error
    assert str(store.get_note(note.id).version) not in result.error  # 不给新版本号，逼它重读
    assert store.get_note(note.id).body_markdown == "别人改过的版本"


def test_update_refuses_other_project_and_global_notes(tmp_path):
    store = _store(tmp_path)
    other = _note(store, "B 的笔记", "b", (), PROJECT_B)
    global_note = store.create_note("跨项目备忘", "g")
    ghost = "f" * 32
    registry = _registry(store, tmp_path)

    texts = set()
    for note_id in (other.id, global_note.id, ghost):
        result = registry.execute(
            "notes_update", {"note_id": note_id, "expected_version": 1, "body_markdown": "x"}
        )
        assert not result.ok
        texts.add(result.error.replace(note_id, "ID"))

    assert texts == {f"{NOT_FOUND_PREFIX}ID（可先用 notes_list 查 id）"}  # 三种情况同一句提示
    assert store.get_note(other.id).body_markdown == "b"
    assert store.get_note(global_note.id).body_markdown == "g"


def test_update_rejects_missing_expected_version(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "笔记")
    result = _registry(store, tmp_path).execute(
        "notes_update", {"note_id": note.id, "body_markdown": "x"}
    )
    assert not result.ok and "expected_version" in result.error
    assert store.get_note(note.id).body_markdown == "正文"


# ---------- notes_delete ----------


def test_delete_removes_note(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "临时笔记")
    registry = _registry(store, tmp_path)

    result = registry.execute(
        "notes_delete", {"note_id": note.id, "expected_title": "临时笔记", "expected_version": 1}
    )

    assert result.ok and "已删除笔记" in result.output
    assert store.get_note(note.id) is None
    assert not registry.execute("notes_read", {"note_id": note.id}).ok


def test_delete_title_mismatch_is_refused(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "要保留的笔记")
    result = _registry(store, tmp_path).execute(
        "notes_delete", {"note_id": note.id, "expected_title": "别的标题", "expected_version": 1}
    )

    assert not result.ok and "标题不匹配" in result.error
    assert store.get_note(note.id) is not None


def test_delete_version_conflict_is_refused(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "笔记", "v1")
    store.update_note(note.id, body_markdown="v2")  # 别人改过，版本变成 2

    result = _registry(store, tmp_path).execute(
        "notes_delete", {"note_id": note.id, "expected_title": "笔记", "expected_version": 1}
    )

    assert not result.ok and "已被其他位置更新" in result.error
    assert store.get_note(note.id) is not None


def test_delete_refuses_other_project_and_global_notes(tmp_path):
    store = _store(tmp_path)
    other = _note(store, "B 的笔记", "b", (), PROJECT_B)
    global_note = store.create_note("跨项目备忘", "g")
    registry = _registry(store, tmp_path)

    for note in (other, global_note):
        result = registry.execute(
            "notes_delete",
            {"note_id": note.id, "expected_title": note.title, "expected_version": 1},
        )
        assert not result.ok and "笔记不存在" in result.error
    assert store.get_note(other.id) is not None
    assert store.get_note(global_note.id) is not None


# ---------- 存储层与适配器 ----------


def test_delete_note_version_guard_is_atomic(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "笔记", "v1")
    store.update_note(note.id, body_markdown="v2")

    assert store.delete_note(note.id, expected_version=1) is False
    assert store.get_note(note.id) is not None
    assert store.delete_note(note.id, expected_version=2) is True
    assert store.get_note(note.id) is None


def test_delete_note_without_version_keeps_old_behaviour(tmp_path):
    store = _store(tmp_path)
    note = _note(store, "笔记")
    assert store.delete_note(note.id) is True
    assert store.delete_note(note.id) is False  # 已不存在


def test_adapter_translates_conflict_before_validation_errors(tmp_path):
    """`NoteConflictError` 是 `ValueError` 的子类：except 顺序写反就会把冲突报成校验错误。"""
    store = _store(tmp_path)
    source = _source(store)
    note = _note(store, "笔记", "v1")
    store.update_note(note.id, body_markdown="v2")

    try:
        source.update_note(note.id, title=None, body_markdown="v3", tags=None, expected_version=1)
    except NoteConflict as exc:
        assert "已被其他位置更新" in str(exc)
    else:  # pragma: no cover - 翻译错了才会走到这里
        raise AssertionError("版本冲突没有被翻译成 NoteConflict")

    try:
        source.update_note(note.id, title="   ", body_markdown=None, tags=None, expected_version=2)
    except NotesWriteError as exc:
        assert not isinstance(exc, NoteConflict)
        assert "标题不能为空" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("校验错误没有被翻译成 NotesWriteError")


def test_store_still_raises_note_conflict_error(tmp_path):
    """适配器只翻译，不改 store 的语义（REST 侧仍要抛 NoteConflictError → 409）。"""
    store = _store(tmp_path)
    note = _note(store, "笔记", "v1")
    store.update_note(note.id, body_markdown="v2")

    try:
        store.update_note(note.id, expected_version=1)
    except NoteConflictError:
        pass
    else:  # pragma: no cover
        raise AssertionError("store 应当继续抛 NoteConflictError")


# ---------- 审计与审批 ----------


def test_audit_keeps_diagnostics_but_not_body(tmp_path):
    audit = AuditLogger(tmp_path / ".routivus" / "audit.log")
    store = _store(tmp_path)
    note = _note(store, "上线检查表", "旧正文")
    registry = _registry(store, tmp_path, audit=audit)

    registry.execute("notes_create", {"title": "新笔记", "body_markdown": "SECRET_CREATE_TOKEN"})
    registry.execute(
        "notes_update",
        {"note_id": note.id, "expected_version": 1, "body_markdown": "SECRET_UPDATE_TOKEN"},
    )
    registry.execute(
        "notes_delete", {"note_id": note.id, "expected_title": "上线检查表", "expected_version": 2}
    )
    log = (tmp_path / ".routivus" / "audit.log").read_text(encoding="utf-8")

    assert "SECRET_CREATE_TOKEN" not in log
    assert "SECRET_UPDATE_TOKEN" not in log
    assert "[19 字已省略]" in log  # 正文位置只剩长度
    assert "上线检查表" in log and "expected_title" in log  # 诊断信息仍在
    assert log.count('"action": "tool_call"') == 3


def test_write_tools_need_approval(tmp_path):
    policy = HITLPolicy(enabled=True)
    assert policy.sensitivity("notes_create") == "confirm"
    assert policy.sensitivity("notes_update") == "confirm"
    assert policy.sensitivity("notes_delete") == "always"
    for tool in WRITE_TOOLS:
        assert policy.requires_approval(tool) is True


# ---------- 端到端 ----------


def _config(tmp_path: Path, workspace: Path) -> ServerConfig:
    return ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )


def test_default_agent_can_crud_notes_end_to_end(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path)
    session = store.create_session(PROJECT_A, title="会话")
    monkeypatch.setattr(
        "routivus.config.settings.load_settings",
        lambda manager: Settings(api_base="https://api.test/v1", api_key="sk-test", model="test-model"),
    )
    monkeypatch.setattr("routivus.llm.factory.create_client", lambda **kwargs: object())
    project = ProjectRecord(
        id=PROJECT_A, name="Alpha", root_path=str(workspace), branch=None, status="active",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )

    agent = _build_default_agent(project, session, notes_source=store)
    tools = agent.tools
    assert set(WRITE_TOOLS) <= set(tools.names())

    created = tools.execute("notes_create", {"title": "上线检查表", "body_markdown": "回滚步骤", "tags": ["ops"]})
    assert created.ok
    note_id = created.output.split("id=", 1)[1].split()[0]

    assert "上线检查表" in tools.execute("notes_list", {}).output
    read = tools.execute("notes_read", {"note_id": note_id})
    version = int(VERSION_RE.search(read.output).group(1))

    updated = tools.execute(
        "notes_update", {"note_id": note_id, "expected_version": version, "body_markdown": "回滚步骤（已更新）"}
    )
    assert updated.ok and "已更新笔记" in updated.output
    assert "回滚步骤（已更新）" in tools.execute("notes_read", {"note_id": note_id}).output

    deleted = tools.execute(
        "notes_delete", {"note_id": note_id, "expected_title": "上线检查表", "expected_version": version + 1}
    )
    assert deleted.ok
    assert "还没有笔记" in tools.execute("notes_list", {}).output


def test_write_toggle_defaults_on_and_reads_env(tmp_path):
    from routivus.config.manager import ConfigManager

    assert Settings().notes_write_enabled is True  # 直接构造的默认值
    base = dict(project_dir=tmp_path / ".routivus", load_env=False)
    assert load_settings(ConfigManager(**base, env={})).notes_write_enabled is True
    assert (
        load_settings(ConfigManager(**base, env={"ROUTIVUS_NOTES_WRITE": "off"})).notes_write_enabled
        is False
    )


def test_create_app_still_binds_store(tmp_path):
    """接线回归：`app.state.agent_factory` 仍是绑定了 store 的 partial（写工具随之启用）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _store(tmp_path)

    app = create_app(
        registry=ProjectRegistry(tmp_path / "projects.json", [workspace]),
        store=store,
        config=_config(tmp_path, workspace),
    )

    factory = app.state.agent_factory
    assert isinstance(factory, functools.partial)
    assert factory.keywords["notes_source"] is store
