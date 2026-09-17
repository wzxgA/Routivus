"""`@` 文件引用（方案 05）的单元与协议级测试。

覆盖方案 §8 的测试要点：解析正例 / 反例、搜索端点（前缀过滤、忽略目录、根一层、
越界被拒）、提示段构造，以及最关键的**落库与注入分离**——消息流里仍是用户原文，
只有发给模型的那一份带提示段。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.files import (
    WorkspaceFileError,
    parse_at_references,
    reference_hint,
    search_paths,
)
from routivus.server.storage import WorkspaceStore


# ==========================================================================
# 替身与脚手架
# ==========================================================================


class _CapturingAgent:
    """记录 `run()` 入参的假 agent：用来验证"发给模型的 prompt 带提示段"。"""

    def __init__(self) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = object()
        self.approval_policy = None
        self.ask_requester = None
        self.prompts: list[str] = []

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        self.prompts.append(content)
        yield AgentEvent(kind="content", text="收到")
        yield AgentEvent(kind="done")


def _client(tmp_path: Path, agent_factory: Any = None) -> TestClient:
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
            agent_factory=agent_factory or (lambda project, session: _CapturingAgent()),
        )
    )


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir(exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def _seed(project: dict, rel: str) -> Path:
    target = Path(project["root_path"]) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    return target


def _receive_until(socket: Any, predicate: Any, limit: int = 120) -> list[dict]:
    received: list[dict] = []
    for _ in range(limit):
        try:
            event = socket.receive_json()
        except Exception:
            break
        received.append(event)
        if predicate(event):
            break
    return received


def _completed(event: dict) -> bool:
    return event.get("type") == "session.status" and event.get("data", {}).get("status") == "completed"


# ==========================================================================
# 解析（白名单式：宁可不认）
# ==========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@src/main.ts", ["src/main.ts"]),
        ("看看 @src/main.ts 里的错误处理", ["src/main.ts"]),
        ("行尾也算 @a/b.py", ["a/b.py"]),
        ("@中文目录/文件.py 处理一下", ["中文目录/文件.py"]),
        ("@a.ts 和 @b/c.ts", ["a.ts", "b/c.ts"]),
        ("重复 @a.ts 再提一次 @a.ts", ["a.ts"]),
        # 标点是边界：中文顿号分隔（高频写法）与半角冒号后的行号都不进路径
        ("@a.ts、@b/c.ts", ["a.ts", "b/c.ts"]),
        ("@src/a.py:42 看这里", ["src/a.py"]),
        ("a@b.com", []),  # `@` 前不是空白边界
        ("@@x", []),  # 路径段以 `@` 开头
        ("@", []),  # 空路径
        ("@../outside.ts", []),  # 越界
        ("@/abs/path.ts", []),  # 绝对路径
        ('@"my file.ts"', []),  # 不做引号转义
    ],
)
def test_parse_at_references(text: str, expected: list[str]) -> None:
    assert parse_at_references(text) == expected


# ==========================================================================
# 提示段
# ==========================================================================


def test_reference_hint_marks_existing_missing_and_dir(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("x", encoding="utf-8")

    hint = reference_hint(tmp_path, "看 @src/main.ts、@src/gone.ts 和 @src")

    assert hint.startswith("[用户提到的项目内文件]")
    assert "- src/main.ts（存在）" in hint
    assert "- src/gone.ts（不存在）" in hint
    assert "- src（目录）" in hint
    # 提示段只给路径与存在性，不夹带文件内容
    assert "read_file" in hint
    assert "\nx\n" not in hint


def test_reference_hint_is_empty_without_refs(tmp_path: Path) -> None:
    assert reference_hint(tmp_path, "普通问题，没有引用任何文件") == ""


# ==========================================================================
# 搜索（纯函数）
# ==========================================================================


def test_search_paths_prefix_filter_and_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "main.js").write_text("x", encoding="utf-8")

    result = search_paths(tmp_path, "src/main")

    assert [entry["path"] for entry in result["entries"]] == ["src/main.py", "src/main.ts"]
    assert result["truncated"] is False


def test_search_paths_empty_query_returns_root_layer_only(tmp_path: Path) -> None:
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "top.ts").write_text("x", encoding="utf-8")

    result = search_paths(tmp_path, "")

    # 只给根下一层：不做递归，`src/deep` 不出现在结果里
    assert sorted(entry["name"] for entry in result["entries"]) == ["src", "top.ts"]


def test_search_paths_caps_results(tmp_path: Path) -> None:
    (tmp_path / "many").mkdir()
    for index in range(8):
        (tmp_path / "many" / f"hit{index}.ts").write_text("x", encoding="utf-8")

    result = search_paths(tmp_path, "hit", limit=3)

    assert len(result["entries"]) == 3
    assert result["truncated"] is True


def test_search_paths_rejects_escaping_query(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceFileError) as excinfo:
        search_paths(tmp_path, "../outside")
    assert excinfo.value.status_code == 422


# ==========================================================================
# 搜索端点（REST）
# ==========================================================================


def test_search_endpoint_returns_entries(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "src/a.ts")
    _seed(project, "src/a.py")

    response = client.get(
        f"/api/projects/{project['id']}/files/search", params={"q": "src/a"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert [entry["path"] for entry in payload["entries"]] == ["src/a.py", "src/a.ts"]
    # 与文件树同一套结构（前端因此不用写第二份解析）
    assert {"name", "path", "type", "size"} <= set(payload["entries"][0])


def test_search_endpoint_rejects_escaping_query(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)

    response = client.get(
        f"/api/projects/{project['id']}/files/search", params={"q": "../secret"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_path"


# ==========================================================================
# 落库与注入分离（方案 §4.1 的关键约束）
# ==========================================================================


def test_prompt_carries_hint_while_message_keeps_raw_text(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    client = _client(tmp_path, agent_factory=lambda project, session: agent)
    project = _project(client)
    _seed(project, "main.ts")
    session = _session(client, project)

    with client.websocket_connect(
        f"/api/ws/projects/{project['id']}/sessions/{session['id']}"
    ) as socket:
        socket.receive_json()  # session.snapshot
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "看看 @main.ts"})
        _receive_until(socket, _completed)

    assert agent.prompts, "agent.run 应被调用"
    sent = agent.prompts[0]
    assert "[用户提到的项目内文件]" in sent
    assert "- main.ts（存在）" in sent

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    user_texts = [item["content"] for item in messages if item["role"] == "user"]
    assert user_texts == ["看看 @main.ts"], "落库必须是用户原文，不能带上提示段"


def test_prompt_unchanged_without_reference(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    client = _client(tmp_path, agent_factory=lambda project, session: agent)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(
        f"/api/ws/projects/{project['id']}/sessions/{session['id']}"
    ) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "普通问题"})
        _receive_until(socket, _completed)

    assert agent.prompts == ["普通问题"]
