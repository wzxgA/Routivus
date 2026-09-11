"""Skill 接线的服务端测试（plans/enhancement/02-skill-integration.md §6）。

覆盖三层：注册表构造与工具注入、system prompt 索引注入、Web 通道（/skill 命令 +
REST 列表与启停）。Skill 解析与只读边界由 tests/test_skill_registry.py、
tests/test_skill_tool.py 覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent, ReActAgent
from routivus.config.manager import ConfigManager
from routivus.config.settings import load_settings
from routivus.server import ProjectRegistry, create_app
from routivus.server.app import _build_skill_registry
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore
from routivus.tool.builtin import build_registry

SKILL_MD = '<!-- routivus-skill: name={name} description="演示规范" -->\n{body}'


def _write_skill(root: Path, name: str, body: str = "只读规范正文") -> None:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(SKILL_MD.format(name=name, body=body), encoding="utf-8")


def _manager(tmp_path: Path) -> ConfigManager:
    return ConfigManager(
        user_dir=tmp_path / "user",
        project_dir=tmp_path / "proj" / ".routivus",
        load_env=False,
    )


# ==========================================================================
# 注册表构造与工具注入
# ==========================================================================


def test_registry_discovers_user_and_project_skills(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    _write_skill(tmp_path / "user" / "skills", "global-demo")
    _write_skill(root / ".routivus" / "skills", "project-demo")

    settings = load_settings(manager)
    registry = _build_skill_registry(manager, root, settings, None)

    assert registry is not None
    names = {info.name: info.source for info in registry.list()}
    assert names["global-demo"] == "user"
    assert names["project-demo"] == "project"


def test_load_skill_tool_is_registered_and_reads_body(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    _write_skill(root / ".routivus" / "skills", "demo", body="唯一规范：先写测试")

    registry = _build_skill_registry(manager, root, load_settings(manager), None)
    tools = build_registry(base_dir=root, skill_registry=registry)

    assert "load_skill" in tools.names()
    assert tools.skill_registry is registry


def test_registry_degrades_when_construction_fails(tmp_path: Path) -> None:
    class _BrokenSettings:
        skills_enabled = True
        skills_max_index_items = 20
        skills_max_index_chars = 4096
        skills_max_chars = 32_000
        skills_max_reference_chars = 16_000
        skills_max_loaded_chars = 64_000

    class _BrokenManager(ConfigManager):
        def __init__(self) -> None:  # pragma: no cover - 只在测试里制造异常
            pass

        @property
        def user_dir(self):  # type: ignore[override]
            raise OSError("user dir unavailable")

    broken = _BrokenManager()
    assert _build_skill_registry(broken, tmp_path, _BrokenSettings(), None) is None


# ==========================================================================
# system prompt 索引注入
# ==========================================================================


def test_agent_system_prompt_carries_skill_index(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    _write_skill(root / ".routivus" / "skills", "demo")
    settings = load_settings(manager)
    registry = _build_skill_registry(manager, root, settings, None)

    agent = ReActAgent(llm=object(), tools=build_registry(base_dir=root), settings=settings, skill_registry=registry)
    prompt = agent.context.base_system_prompt.content
    assert "demo" in prompt
    assert "可用 Skill" in prompt  # skill/prompt.py 的索引标题

    # /skill disable 之后下一轮生效：注册表 reload 后 prompt 同步收缩
    registry.set_enabled("demo", False)
    agent._refresh_skill_index()
    assert "demo" not in agent.context.base_system_prompt.content


def test_agent_without_registry_keeps_base_prompt(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    settings = load_settings(manager)
    agent = ReActAgent(llm=object(), tools=build_registry(base_dir=root), settings=settings)
    assert agent.context.base_system_prompt.content == agent._base_system_prompt


# ==========================================================================
# Web 通道：REST + /skill 命令
# ==========================================================================


class _SkillAgent:
    """/skill 命令只需要 skill_registry（其余属性保持最小面）。"""

    def __init__(self, registry: Any) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = object()
        self.config_manager = None
        self.approval_policy = None
        self.skill_registry = registry


def _client(tmp_path: Path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    return TestClient(
        create_app(
            registry=registry,
            store=WorkspaceStore(tmp_path / "workspace.sqlite3"),
            config=config,
            agent_factory=lambda project, session: _SkillAgent(
                _build_skill_registry(
                    _manager(tmp_path),
                    Path(project.root_path),
                    load_settings(_manager(tmp_path)),
                    None,
                )
            ),
        )
    )


def _project(client: TestClient) -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / "alpha"
    root.mkdir(exist_ok=True)
    response = client.post("/api/projects", json={"name": "Alpha", "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def test_rest_lists_and_toggles_user_skill(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _write_skill(tmp_path / "userdata" / "skills", "demo")

    listed = client.get("/api/skills").json()
    assert [item["name"] for item in listed] == ["demo"]
    assert listed[0]["source"] == "user" and listed[0]["enabled"] is True

    disabled = client.post("/api/skills/demo/disable").json()
    assert disabled[0]["enabled"] is False
    enabled = client.post("/api/skills/demo/enable").json()
    assert enabled[0]["enabled"] is True

    assert client.post("/api/skills/nope/enable").status_code == 404


def test_rest_lists_project_skill_with_project_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _write_skill(Path(project["root_path"]) / ".routivus" / "skills", "proj-demo")

    global_list = client.get("/api/skills").json()
    assert [item["name"] for item in global_list] == []

    scoped = client.get("/api/skills", params={"project_id": project["id"]}).json()
    assert [item["name"] for item in scoped] == ["proj-demo"]
    assert scoped[0]["source"] == "project"


def test_web_skill_command_lists_and_toggles(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)
    _write_skill(Path(project["root_path"]) / ".routivus" / "skills", "proj-demo")

    def command_result(socket: Any, content: str) -> dict:
        socket.send_json({"type": "user_message", "content": content, "request_id": f"r-{content}"})
        events: list[dict] = []
        for _ in range(60):
            event = socket.receive_json()
            events.append(event)
            if event.get("type") == "command.executed":
                break
        return events

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()  # snapshot
        events = command_result(socket, "/skill list")
        receipt = client.get(f"/api/sessions/{session['id']}/messages").json()[-1]["content"]
        assert "proj-demo" in receipt

        command_result(socket, "/skill disable proj-demo")
        receipt = client.get(f"/api/sessions/{session['id']}/messages").json()[-1]["content"]
        assert "禁用" in receipt or "已禁用" in receipt
        assert events  # 命令通道有回执事件
