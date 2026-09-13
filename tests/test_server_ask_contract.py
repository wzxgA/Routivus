"""ask_user 的 Web 线上契约（回归钉子）。

背景：ask 卡曾因前后端契约错位把整个应用打崩——前端按 name/label/options: string[]
渲染，服务端实际发的是 key/question/options: [{label, value}]（选项对象被当成
React 子节点，未捕获的渲染错误卸载整棵树，页面只剩纯底色）。

本文件钉住 `approval.requested`（ask 分支）的真实载荷形状与答案回灌路径：
字段必须是 key/question/options[{label, value}]/allow_custom/default/required，
答案必须按 field.key 回填。改 AskField/AskRequest 时此文件会先红。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.ask.models import AskField, AskOption, AskRequest
from routivus.config.settings import Settings
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


class _AskAgent:
    """yield 一次带选项的 ask_user，等 ask_requester 回答后把答案原样回显。"""

    def __init__(self) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = Settings(api_base="https://api.test/v1", api_key="sk-test", model="m")
        self.approval_policy = None
        self.memory_manager = None
        self.ask_requester: Any = None
        self.received: list[dict[str, str] | None] = []

    def clear(self) -> None:
        return None

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        ask = AskRequest.new(
            prompt="重构前需要确认两个点",
            fields=(
                AskField(
                    key="verify",
                    question="验证方式",
                    required=True,
                    options=(
                        # 真实链路里 _build_ask_request 会把缺省 value 归一成 label，
                        # 所以线上 value 恒非空；这里显式写出等价形态
                        AskOption(label="跑现有测试", value="跑现有测试"),
                        AskOption(label="补新测试", value="add"),
                    ),
                ),
                AskField(key="notes", question="补充说明"),
            ),
        )
        yield AgentEvent(kind="ask_user", ask=ask)
        answer = await self.ask_requester(ask)
        self.received.append(answer)
        yield AgentEvent(kind="content", text=json.dumps(answer, ensure_ascii=False))
        yield AgentEvent(kind="done")


def _client(tmp_path: Path) -> tuple[TestClient, _AskAgent]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    agent = _AskAgent()
    client = TestClient(
        create_app(
            registry=registry,
            store=WorkspaceStore(tmp_path / "workspace.sqlite3"),
            config=config,
            agent_factory=lambda project, session: agent,
        )
    )
    return client, agent


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


def _ws(project: dict, session: dict) -> str:
    return f"/api/ws/projects/{project['id']}/sessions/{session['id']}"


def test_ask_payload_shape_and_answer_roundtrip(tmp_path: Path) -> None:
    client, agent = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()  # snapshot
        socket.send_json({"type": "user_message", "content": "重构登录", "request_id": "r1"})

        # 权威载荷来自 ApprovalBridge（带 approval_id / timeout）。转发器还会为同
        # 一个 ask_user 事件先发一条不带 id 的同名事件，这里只认带 id 的那条。
        requested: dict[str, Any] | None = None
        for _ in range(60):
            event = socket.receive_json()
            data = event.get("data") or {}
            if event.get("type") == "approval.requested" and data.get("approval_id"):
                requested = data
                break
        assert requested is not None, "未收到带 approval_id 的 approval.requested"

        assert requested["kind"] == "ask"
        assert requested["timeout"] > 0
        ask = requested["ask"]
        assert ask["prompt"] == "重构前需要确认两个点"
        assert ask["fields"][0] == {
            "key": "verify",
            "question": "验证方式",
            "options": [
                {"label": "跑现有测试", "value": "跑现有测试"},
                {"label": "补新测试", "value": "add"},
            ],
            "allow_custom": True,
            "default": "",
            "required": True,
        }
        assert ask["fields"][1]["options"] == []

        # 答案按 field.key 回填；agent 侧应原样收到这份 dict
        socket.send_json(
            {
                "type": "ask_answer",
                "request_id": "r2",
                "approval_id": requested["approval_id"],
                "answers": {"verify": "add", "notes": "无"},
            }
        )
        answered = None
        for _ in range(60):
            event = socket.receive_json()
            data = event.get("data") or {}
            if event.get("type") == "message.delta" and data.get("kind") == "content":
                answered = data["text"]
                break
        assert answered is not None
        assert json.loads(answered) == {"verify": "add", "notes": "无"}

    assert agent.received == [{"verify": "add", "notes": "无"}]


def test_ask_cancel_resolves_as_skipped(tmp_path: Path) -> None:
    """跳过 = fail-closed：agent 收到 None，不代选默认值。"""
    client, agent = _client(tmp_path)
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "content": "重构登录", "request_id": "r1"})
        requested: dict[str, Any] | None = None
        for _ in range(60):
            event = socket.receive_json()
            data = event.get("data") or {}
            if event.get("type") == "approval.requested" and data.get("approval_id"):
                requested = data
                break
        assert requested is not None

        socket.send_json(
            {
                "type": "ask_cancel",
                "request_id": "r2",
                "approval_id": requested["approval_id"],
            }
        )
        answered = None
        for _ in range(60):
            event = socket.receive_json()
            data = event.get("data") or {}
            if event.get("type") == "message.delta" and data.get("kind") == "content":
                answered = data["text"]
                break
        # agent 收到 None，json.dumps 后是 "null"
        assert answered == "null"

    assert agent.received == [None]
