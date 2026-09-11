"""Composer 命令补全端点（GET /api/completions）的协议级测试。

补全引擎本身（`routivus/cli/completion.py`）已有独立单测，这里验的是**服务端
接线**：白名单过滤（只提示桌面端真正会执行的命令）、按会话所属项目展开
工作区路径候选、会话缺失时的优雅降级。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from routivus.server import ProjectRegistry, create_app
from routivus.server.completions import completion_payload
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore

COMPLETIONS = "/api/completions"


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
            agent_factory=lambda project, session: object(),
        )
    )


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir(exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "auth.py").write_text("x = 1", encoding="utf-8")
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


# ==========================================================================
# 纯载荷：白名单与解析边界
# ==========================================================================


def test_payload_ignores_non_command_input() -> None:
    payload = completion_payload("帮我修复登录 bug")
    # replace 区间指向行尾：即便后续误用也不至于改写用户文本。
    assert payload == {
        "is_command": False,
        "replace_start": len("帮我修复登录 bug"),
        "replace_end": len("帮我修复登录 bug"),
        "candidates": [],
    }


def test_payload_prefix_matches_whitelist_only() -> None:
    # "/pl" 只提示白名单里的 /plan；/provider、/path 等 TUI 命令不出现。
    payload = completion_payload("/pl")
    assert [cand["insert_text"] for cand in payload["candidates"]] == ["/plan"]
    assert payload["is_command"] is True

    # 刚敲 "/"：列出白名单内的全部命令。
    assert [cand["insert_text"] for cand in completion_payload("/")["candidates"]] == [
        "/plan",
        "/team",
    ]


def test_payload_resolved_command_keeps_subcommands() -> None:
    payload = completion_payload("/team ")
    assert [cand["insert_text"] for cand in payload["candidates"]] == ["run", "resume"]


def test_payload_hides_non_desktop_command() -> None:
    # /model 在桌面聊天里不会被执行（走配置页），不能给出假提示。
    assert completion_payload("/model dee")["candidates"] == []
    assert completion_payload("/model")["candidates"] == []


def test_payload_replace_span_points_at_current_token() -> None:
    payload = completion_payload("/team res")
    assert payload["replace_start"] == len("/team ")
    assert payload["replace_end"] == len("/team res")
    assert payload["candidates"][0]["insert_text"] == "resume"


# ==========================================================================
# 端点：项目路径候选与会话降级
# ==========================================================================


def test_endpoint_offers_paths_for_write_scope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    query = "/team resume t4 --write-scope sr"
    response = client.get(COMPLETIONS, params={"q": query, "cursor": len(query), "session_id": session["id"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_command"] is True
    texts = [cand["insert_text"] for cand in payload["candidates"]]
    # 项目 root 下的 src/ 目录按前缀 "sr" 展开（拒绝规则与 .env/.git 等由引擎负责）。
    assert "src/" in texts
    assert all(not text.startswith("/") for text in texts)


def test_endpoint_degrades_without_session(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _project(client)

    response = client.get(COMPLETIONS, params={"q": "/pl"})
    assert response.status_code == 200
    payload = response.json()
    assert [cand["insert_text"] for cand in payload["candidates"]] == ["/plan"]

    # 会话不存在也不 404：只是拿不到项目根，静态候选照常返回。
    response = client.get(COMPLETIONS, params={"q": "/pl", "session_id": "missing"})
    assert response.status_code == 200
    assert response.json()["candidates"]

