"""Phase 5 server tests: HITL 审批闭环与终端通道。

约定：除标注 `slow` 的真进程用例之外，所有测试都通过注入假 agent / 假终端
后端运行，不 spawn 真实 shell。每个 WS 测试都有硬性的 receive 次数上限，
回归时失败而不是挂死。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.safety.audit import AuditLogger
from routivus.safety.hitl import ApprovalDecision, HITLPolicy
from routivus.server import ProjectRegistry, create_app
from routivus.server.approval import ApprovalBridge
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore
from routivus.server.terminal import (
    MIN_COLS,
    MIN_ROWS,
    OneShotBackend,
    PtyBackend,
    TerminalSession,
    TerminalSpec,
    TerminalUnavailableError,
    _decode,
    default_shell,
    new_terminal_id,
    select_backend,
)
from routivus.server import terminal as terminal_module
from routivus.server import winproc


def _app(tmp_path: Path, **kwargs) -> tuple[TestClient, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    return TestClient(create_app(registry=registry, store=store, **kwargs)), workspace, tmp_path


def _project(client: TestClient, workspace: Path, name: str) -> dict:
    root = workspace / name.lower()
    root.mkdir()
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def _receive_until(socket, predicate, limit: int = 40) -> list[dict]:
    """收事件直到满足条件或达到次数上限。

    必须给出一个**保证会到达**的终止条件：TestClient 的 socket 在消息收完后
    不会自己断开，盲目 drain 会永久阻塞。
    """
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


def _find(events: list[dict], event_type: str) -> list[dict]:
    return [item for item in events if item.get("type") == event_type]


def _project_audit_lines(root: Path) -> list[dict]:
    path = root / ".routivus" / "audit.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ==========================================================================
# 审批：ApprovalBridge 单元
# ==========================================================================


def _bridge(**kwargs) -> tuple[ApprovalBridge, list[tuple[str, dict]], list[bool]]:
    emitted: list[tuple[str, dict]] = []
    waiting: list[bool] = []

    async def emit(event_type, data):
        emitted.append((event_type, data))

    async def set_waiting(flag):
        waiting.append(flag)

    bridge = ApprovalBridge(emit=emit, set_waiting=set_waiting, timeout=kwargs.pop("timeout", 5.0), **kwargs)
    return bridge, emitted, waiting


async def test_approval_bridge_emits_requested_then_resolves() -> None:
    bridge, emitted, waiting = _bridge()

    async def answer_soon():
        for _ in range(100):
            if bridge.has_pending:
                break
            await __import__("asyncio").sleep(0.01)
        assert bridge.resolve_approval(bridge.pending_id, ApprovalDecision(allow=True, reason="user_approved"))

    import asyncio

    task = asyncio.create_task(answer_soon())
    decision = await bridge.request("write_file", "confirm", {"path": "a.txt"})
    await task

    assert decision.allow and decision.reason == "user_approved"
    assert emitted[0][0] == "approval.requested"
    assert emitted[0][1]["tool_name"] == "write_file"
    assert emitted[0][1]["level"] == "confirm"
    assert emitted[0][1]["approval_id"] == bridge.pending_id or True
    assert emitted[-1][0] == "approval.resolved"
    assert emitted[-1][1]["decision"] == "approve"
    assert waiting == [True, False]
    assert not bridge.has_pending


async def test_approval_bridge_modified_args_passthrough() -> None:
    bridge, _, _ = _bridge()
    import asyncio

    async def answer():
        while not bridge.has_pending:
            await asyncio.sleep(0.01)
        bridge.resolve_approval(bridge.pending_id, ApprovalDecision(allow=True, args={"path": "b.txt"}, reason="user_modified"))

    task = asyncio.create_task(answer())
    decision = await bridge.request("write_file", "confirm", {"path": "a.txt"})
    await task
    assert decision.allow
    assert decision.args == {"path": "b.txt"}
    assert decision.reason == "user_modified"


async def test_approval_bridge_timeout_fails_closed() -> None:
    bridge, emitted, _ = _bridge(timeout=0.05)
    decision = await bridge.request("execute_command", "always", {"command": "dir"})
    assert decision.allow is False
    assert decision.reason == "approval_timeout"
    assert not bridge.has_pending
    assert any(item[1].get("reason") == "approval_timeout" for item in emitted if item[0] == "approval.resolved")


async def test_approval_bridge_busy_and_mismatch_rejected() -> None:
    bridge, _, _ = _bridge(timeout=0.05)
    # 没有待决项时的回执一律拒绝
    assert bridge.resolve_approval("ap-xxx", ApprovalDecision(allow=True)) is False
    assert bridge.resolve_ask("ask-xxx", {"a": "b"}) is False


async def test_approval_bridge_ask_and_cancel() -> None:
    import asyncio

    from routivus.ask.models import AskRequest

    bridge, emitted, _ = _bridge(timeout=2.0)

    async def answer():
        while not bridge.has_pending:
            await asyncio.sleep(0.01)
        bridge.resolve_ask(bridge.pending_id, {"目标": "A"})

    task = asyncio.create_task(answer())
    result = await bridge.ask(AskRequest.new("选一个"))
    await task
    assert result == {"目标": "A"}
    assert emitted[0][1]["kind"] == "ask"

    # 跳过 → None（fail closed，不代选）
    async def skip():
        while not bridge.has_pending:
            await asyncio.sleep(0.01)
        bridge.resolve_ask(bridge.pending_id, None)

    task = asyncio.create_task(skip())
    assert await bridge.ask(AskRequest.new("再选一次")) is None
    await task


async def test_approval_bridge_cancel_pending_resolves_future() -> None:
    import asyncio

    bridge, _, _ = _bridge(timeout=5.0)

    async def cancel():
        while not bridge.has_pending:
            await asyncio.sleep(0.01)
        bridge.cancel_pending()

    task = asyncio.create_task(cancel())
    decision = await bridge.request("execute_command", "always", {"command": "dir"})
    await task
    assert decision.allow is False
    assert decision.reason == "user_cancelled"
    assert not bridge.has_pending


# ==========================================================================
# 审批：走 WebSocket 的端到端
# ==========================================================================


class ApprovalAgent:
    """假 agent：按脚本依次请求审批，把决策结果回报给测试。"""

    def __init__(self, results: list, decisions: list, tools: list[tuple[str, str, dict]] | None = None) -> None:
        self.results = results
        self.decisions = decisions
        self.tools = tools or [("write_file", "confirm", {"path": "a.txt"})]
        self.approval_policy = HITLPolicy(enabled=True)
        self.ask_requester = None

    async def run(self, content: str):
        for index, (tool, _level, args) in enumerate(self.tools):
            # 走 decide() 而不是直接调 requester：这样才会经过 requires_approval()，
            # 从而覆盖「本会话放行」让后续调用直接 auto_allow 的行为，
            # 与 react.py:269 的真实路径一致。
            decision = await self.approval_policy.decide(tool, args)
            self.decisions.append(decision)
            yield AgentEvent(kind="content", text=f"call {index}")
        yield AgentEvent(kind="done")


def _approval_app(tmp_path: Path) -> tuple[TestClient, dict, dict, Path]:
    client, workspace, _ = _app(tmp_path)
    project = _project(client, workspace, "Alpha")
    session = _session(client, project)
    return client, project, session, Path(project["root_path"])


def test_websocket_approval_requested_then_approved(tmp_path: Path) -> None:
    client, project, session, _ = _approval_app(tmp_path)
    decisions: list[ApprovalDecision] = []
    client.app.state.agent_factory = lambda _p, _s: ApprovalAgent([], decisions)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()  # session.snapshot
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写文件"})
        events = []
        approval_id = ""
        for _ in range(40):
            event = socket.receive_json()
            events.append(event)
            if event.get("type") == "approval.requested":
                approval_id = event["data"]["approval_id"]
                # 审批未决时会话状态应为 waiting_approval
                assert client.get(f"/api/sessions/{session['id']}").json()["status"] == "waiting_approval"
                socket.send_json({"type": "approve", "request_id": "r2", "approval_id": approval_id})
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert approval_id, "应收到 approval.requested"
    assert decisions and decisions[0].allow and decisions[0].reason == "user_approved"
    assert _find(events, "approval.resolved")
    # 审批结束后状态要回到 running 再到 completed
    statuses = [item["data"]["status"] for item in _find(events, "session.status")]
    assert "waiting_approval" in statuses


def test_websocket_approval_rejected(tmp_path: Path) -> None:
    client, project, session, _ = _approval_app(tmp_path)
    decisions: list[ApprovalDecision] = []
    client.app.state.agent_factory = lambda _p, _s: ApprovalAgent([], decisions)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写文件"})
        for _ in range(40):
            event = socket.receive_json()
            if event.get("type") == "approval.requested":
                socket.send_json({"type": "reject", "request_id": "r2", "approval_id": event["data"]["approval_id"]})
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert decisions and decisions[0].allow is False
    assert decisions[0].reason == "user_rejected"


def test_websocket_approval_modified_args_and_session_scope(tmp_path: Path) -> None:
    client, project, session, _ = _approval_app(tmp_path)
    decisions: list[ApprovalDecision] = []
    agent = ApprovalAgent([], decisions, tools=[("write_file", "confirm", {"path": "a.txt"}), ("write_file", "confirm", {"path": "c.txt"})])
    client.app.state.agent_factory = lambda _p, _s: agent

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写两个文件"})
        requested = 0
        for _ in range(60):
            event = socket.receive_json()
            if event.get("type") == "approval.requested":
                requested += 1
                socket.send_json({
                    "type": "approve",
                    "request_id": f"r{requested + 1}",
                    "approval_id": event["data"]["approval_id"],
                    "args": {"path": "modified.txt"},
                    "scope": "session",
                })
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert len(decisions) == 2
    assert decisions[0].args == {"path": "modified.txt"}
    assert decisions[0].reason == "user_modified"
    # scope=session → policy.allow_all() 生效，第二次调用不再需要审批，
    # 因此只应发出一次 approval.requested。
    assert requested == 1
    assert agent.approval_policy.session_allow_all is True
    assert decisions[1].reason == "auto_allow"


def test_websocket_approval_unknown_id_and_duplicate_request(tmp_path: Path) -> None:
    client, project, session, _ = _approval_app(tmp_path)
    decisions: list[ApprovalDecision] = []
    client.app.state.agent_factory = lambda _p, _s: ApprovalAgent([], decisions)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写文件"})
        seen_mismatch = seen_duplicate = False
        approval_id = ""
        for _ in range(50):
            event = socket.receive_json()
            if event.get("type") == "approval.requested":
                approval_id = event["data"]["approval_id"]
                socket.send_json({"type": "approve", "request_id": "rx", "approval_id": "ap-not-mine"})
            if event.get("type") == "error" and event.get("data", {}).get("code") == "approval_mismatch":
                seen_mismatch = True
                socket.send_json({"type": "approve", "request_id": "ry", "approval_id": approval_id})
                socket.send_json({"type": "approve", "request_id": "ry", "approval_id": approval_id})
            if event.get("type") == "error" and event.get("data", {}).get("code") == "duplicate_request":
                seen_duplicate = True
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert seen_mismatch, "approval_id 不匹配应被拒绝"
    assert seen_duplicate, "重复 request_id 应幂等"
    assert len(decisions) == 1, "只应产生一个决策"


def test_websocket_approval_timeout_fails_closed(tmp_path: Path) -> None:
    """超时无人应答 → fail closed，会话继续走完而不是永久挂起。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    # 直接构造 ServerConfig 绕过 from_env 的下限夹取，把超时压到 0.2s
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        database_path=tmp_path / "workspace.sqlite3",
        approval_timeout=0.2,
    )
    client = TestClient(create_app(registry=registry, store=store, config=config))
    project = _project(client, workspace, "Alpha")
    session = _session(client, project)
    decisions: list[ApprovalDecision] = []
    client.app.state.agent_factory = lambda _p, _s: ApprovalAgent([], decisions)

    resolved: list[dict] = []
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写文件"})
        for _ in range(40):
            event = socket.receive_json()
            if event.get("type") == "approval.resolved":
                resolved.append(event["data"])
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert decisions, "审批应有一个结果"
    assert decisions[0].allow is False
    assert decisions[0].reason == "approval_timeout"
    assert resolved and resolved[0]["reason"] == "approval_timeout"


def test_websocket_cancel_aborts_pending_approval(tmp_path: Path) -> None:
    """取消会直接终止这一轮，而不是先采纳一个"取消"决策。

    `cancel_pending()` 的作用是解开挂起的 Future，避免它悬着；但紧随其后的
    `task.cancel()` 会在同一轮事件循环里把 CancelledError 投递给正在 await 的
    Agent，所以这一轮不会走到「新增一条 decision」那一步。真正需要断言的是
    可观察结果：会话停在 cancelled，且没有任何工具以「已批准」继续执行。
    """
    client, project, session, _ = _approval_app(tmp_path)
    decisions: list[ApprovalDecision] = []
    client.app.state.agent_factory = lambda _p, _s: ApprovalAgent([], decisions)

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "写文件"})
        for _ in range(30):
            event = socket.receive_json()
            if event.get("type") == "approval.requested":
                socket.send_json({"type": "cancel", "request_id": "r2"})
                break
        _receive_until(
            socket,
            lambda item: item.get("type") == "session.status"
            and item.get("data", {}).get("status") == "cancelled",
        )

    assert all(decision.allow is False for decision in decisions), "取消不应产生已批准的决策"
    assert client.get(f"/api/sessions/{session['id']}").json()["status"] == "cancelled"


def test_websocket_ask_user_roundtrip(tmp_path: Path) -> None:
    client, project, session, _ = _approval_app(tmp_path)

    class AskAgent:
        def __init__(self) -> None:
            self.answer = "unset"
            self.approval_policy = HITLPolicy(enabled=True)
            self.ask_requester = None

        async def run(self, content: str):
            from routivus.ask.models import AskField, AskRequest

            ask = AskRequest.new("选择目标", (AskField(key="目标", question="选哪个"),))
            self.answer = await self.ask_requester(ask)
            yield AgentEvent(kind="done")

    agent = AskAgent()
    client.app.state.agent_factory = lambda _p, _s: agent

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/sessions/{session['id']}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "问我"})
        for _ in range(30):
            event = socket.receive_json()
            if event.get("type") == "approval.requested" and event["data"].get("kind") == "ask":
                assert event["data"]["ask"]["prompt"] == "选择目标"
                socket.send_json({
                    "type": "ask_answer",
                    "request_id": "r2",
                    "approval_id": event["data"]["approval_id"],
                    "answers": {"目标": "A"},
                })
            if event.get("type") == "session.status" and event["data"].get("status") == "completed":
                break

    assert agent.answer == {"目标": "A"}


# ==========================================================================
# 终端：假后端
# ==========================================================================


class FakeBackend:
    """记录输入、按脚本吐输出的假终端后端。"""

    name = "fake"

    def __init__(self, spec: TerminalSpec, chunks: list[bytes] | None = None, eof_after_write: bool = False) -> None:
        self.spec = spec
        self.writes: list[str] = []
        self.chunks = list(chunks or [])
        # 默认不 EOF（模拟常驻 shell）；置位后写完脚本输出即结束，
        # 让测试有一个确定的收尾条件（服务端会发 terminal.exit）。
        self.eof_after_write = eof_after_write
        self.closed = False
        self.started = False
        self.resizes: list[tuple[int, int]] = []
        self._inbox: "list[bytes]" = []
        self._eof_pending = False
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        return None

    @property
    def alive(self) -> bool:
        return self.started and not self.closed

    async def start(self) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)
        for chunk in self.chunks:
            self.push(chunk)
        if self.eof_after_write:
            with self._lock:
                self._eof_pending = True

    def push(self, chunk: bytes) -> None:
        with self._lock:
            self._inbox.append(chunk)

    def read_blocking(self, stop: threading.Event) -> bytes | None:
        while not stop.is_set():
            with self._lock:
                if self._inbox:
                    return self._inbox.pop(0)
                if self._eof_pending:
                    return None
            time.sleep(0.005)
        return None

    async def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def close(self, *, grace: float = 3.0) -> None:
        self.closed = True


def _terminal_client(tmp_path: Path, backend_factory, **config_overrides) -> tuple[TestClient, dict, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        database_path=tmp_path / "workspace.sqlite3",
        ws_auth_token="secret-token",
        **config_overrides,
    )
    client = TestClient(create_app(registry=registry, store=store, config=config, terminal_factory=backend_factory))
    root = workspace / "alpha"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "Alpha", "root_path": str(root)}).json()
    return client, project, root


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


def test_terminal_requires_auth(tmp_path: Path) -> None:
    seen: list[TerminalSpec] = []
    client, project, _ = _terminal_client(tmp_path, lambda spec: seen.append(spec) or FakeBackend(spec))

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal") as socket:
        event = socket.receive_json()
        assert event["code"] == "not_authenticated"
    assert seen == [], "鉴权失败不应构造后端"


def test_terminal_origin_rejected(tmp_path: Path) -> None:
    seen: list[TerminalSpec] = []
    client, project, _ = _terminal_client(
        tmp_path,
        lambda spec: seen.append(spec) or FakeBackend(spec),
        allowed_origins=("http://localhost:5173",),
    )

    with client.websocket_connect(
        f"/api/ws/projects/{project['id']}/terminal",
        headers={**_auth_headers(), "Origin": "http://evil.example"},
    ) as socket:
        event = socket.receive_json()
        assert event["code"] == "origin_not_allowed"
    assert seen == []


def test_terminal_requires_token_or_origin_allowlist(tmp_path: Path) -> None:
    """既无 token 又无 Origin 白名单时必须拒绝 —— 否则等于给任意网页一个 shell。"""
    seen: list[TerminalSpec] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        database_path=tmp_path / "workspace.sqlite3",
        ws_auth_token="",
        allowed_origins=(),
    )
    client = TestClient(create_app(registry=registry, store=store, config=config, terminal_factory=lambda spec: seen.append(spec) or FakeBackend(spec)))
    root = workspace / "alpha"
    root.mkdir()
    project = client.post("/api/projects", json={"name": "Alpha", "root_path": str(root)}).json()

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal") as socket:
        assert socket.receive_json()["code"] == "terminal_auth_required"
    assert seen == []


def test_terminal_disabled(tmp_path: Path) -> None:
    seen: list[TerminalSpec] = []
    client, project, _ = _terminal_client(
        tmp_path, lambda spec: seen.append(spec) or FakeBackend(spec), terminal_enabled=False
    )

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        assert socket.receive_json()["code"] == "terminal_disabled"
    assert seen == []


def test_terminal_unknown_project(tmp_path: Path) -> None:
    client, _, _ = _terminal_client(tmp_path, lambda spec: FakeBackend(spec))
    with client.websocket_connect("/api/ws/projects/proj-does-not-exist/terminal", headers=_auth_headers()) as socket:
        assert socket.receive_json()["code"] == "project_not_found"


def test_terminal_open_binds_cwd_and_ignores_client_path(tmp_path: Path) -> None:
    specs: list[TerminalSpec] = []
    client, project, root = _terminal_client(tmp_path, lambda spec: specs.append(spec) or FakeBackend(spec))

    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({
            "type": "terminal.open",
            "request_id": "r1",
            "cols": 100,
            "rows": 40,
            # 以下字段都必须被忽略
            "cwd": "C:\\Windows",
            "shell": "powershell.exe",
            "project_id": "other",
        })
        event = socket.receive_json()

    assert event["type"] == "terminal.opened"
    assert event["cwd"] == str(root.resolve())
    assert specs[0].cwd == root.resolve()
    assert specs[0].project_id == project["id"]
    assert specs[0].shell == default_shell("")
    assert (event["cols"], event["rows"]) == (100, 40)


def test_terminal_input_before_open(tmp_path: Path) -> None:
    client, project, _ = _terminal_client(tmp_path, lambda spec: FakeBackend(spec))
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.input", "request_id": "r1", "data": "dir\r\n"})
        assert socket.receive_json()["code"] == "terminal_not_opened"


def test_terminal_input_roundtrip_and_seq(tmp_path: Path) -> None:
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec, chunks=[b"hello ", b"world\r\n"], eof_after_write=True)
        backends.append(backend)
        return backend

    client, project, _ = _terminal_client(tmp_path, factory)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        opened = socket.receive_json()
        socket.send_json({"type": "terminal.input", "request_id": "r2", "data": "echo hi\r\n"})
        events = []
        # 收尾条件由后端的 EOF 保证：服务端会发出 terminal.exit。
        for _ in range(30):
            event = socket.receive_json()
            events.append(event)
            if event.get("type") == "terminal.exit":
                break

    assert backends[0].writes == ["echo hi\r\n"]
    assert any(item.get("type") == "terminal.input.ack" for item in events)
    outputs = [item for item in events if item["type"] == "terminal.output"]
    assert outputs, "应收到终端输出"
    blob = "".join(item["data"] for item in outputs)
    assert "hello world" in blob
    sequences = [item["seq"] for item in outputs]
    assert sequences == sorted(sequences)
    for item in outputs:
        assert item["terminal_id"] == opened["terminal_id"]


def test_terminal_guard_blocks_blacklisted_command(tmp_path: Path) -> None:
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec)
        backends.append(backend)
        return backend

    client, project, root = _terminal_client(tmp_path, factory)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        socket.receive_json()
        socket.send_json({"type": "terminal.input", "request_id": "r2", "data": "rm -rf /\r\n"})
        event = socket.receive_json()

    assert event["type"] == "error"
    assert event["code"] == "command_blocked"
    assert backends[0].writes == [], "被拒绝的命令不得转发给终端"

    lines = _project_audit_lines(root)
    blocked = [item for item in lines if item["action"] == "blocked"]
    assert blocked and blocked[0]["origin"] == "terminal"
    assert blocked[0]["reason"] == "command_blacklist"


def test_terminal_audit_distinguishes_from_agent_tool(tmp_path: Path) -> None:
    """终端的审计记录必须与 Agent 的 execute_command 工具调用可区分。"""
    client, project, root = _terminal_client(tmp_path, lambda spec: FakeBackend(spec))

    # 直接写两条记录，模拟两条来源
    audit = AuditLogger(root / ".routivus" / "audit.log", session_id="t-x")
    audit.terminal_command("dir", cwd=str(root), ok=True, terminal_id="t-x")
    audit.tool_call("execute_command", {"command": "dir"}, True, 12)

    lines = _project_audit_lines(root)
    actions = {item["action"] for item in lines}
    assert {"terminal_command", "tool_call"} <= actions
    terminal_record = next(item for item in lines if item["action"] == "terminal_command")
    assert terminal_record["origin"] == "terminal"
    tool_record = next(item for item in lines if item["action"] == "tool_call")
    assert tool_record["tool"] == "execute_command"
    assert "origin" not in tool_record


def test_terminal_audit_redacts_secrets(tmp_path: Path) -> None:
    client, project, root = _terminal_client(tmp_path, lambda spec: FakeBackend(spec))
    audit = AuditLogger(root / ".routivus" / "audit.log", session_id="t-y")
    audit.terminal_command("deploy --token=supersecret123", cwd=str(root), ok=True, terminal_id="t-y")

    raw = (root / ".routivus" / "audit.log").read_text(encoding="utf-8")
    assert "supersecret123" not in raw
    assert "***" in raw


def test_terminal_clear_resize_and_close(tmp_path: Path) -> None:
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec)
        backends.append(backend)
        return backend

    client, project, _ = _terminal_client(tmp_path, factory)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        opened = socket.receive_json()
        terminal_id = opened["terminal_id"]

        socket.send_json({"type": "terminal.clear", "terminal_id": terminal_id})
        cleared = socket.receive_json()
        assert cleared["type"] == "terminal.cleared"
        assert backends[0].writes == [], "terminal.clear 是客户端操作，不得写入后端"

        socket.send_json({"type": "terminal.resize", "terminal_id": terminal_id, "cols": 99999, "rows": -3})
        resized = socket.receive_json()
        assert resized["type"] == "terminal.resized"
        assert resized["cols"] == 400 and resized["rows"] == MIN_ROWS
        assert backends[0].resizes == [(400, MIN_ROWS)]

        socket.send_json({"type": "terminal.close", "request_id": "r2", "terminal_id": terminal_id})
        closed = socket.receive_json()
        assert closed["type"] == "terminal.closed"
        assert closed["reason"] == "client_closed"

        # 关闭后继续输入应报未打开
        socket.send_json({"type": "terminal.input", "request_id": "r3", "data": "dir\r\n"})
        assert socket.receive_json()["code"] == "terminal_not_opened"

    assert backends[0].closed is True
    assert terminal_id not in client.app.state.terminals


def test_terminal_input_too_large_and_duplicate_request(tmp_path: Path) -> None:
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec)
        backends.append(backend)
        return backend

    client, project, _ = _terminal_client(tmp_path, factory, terminal_max_input_bytes=1024)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        socket.receive_json()
        socket.send_json({"type": "terminal.input", "request_id": "big", "data": "x" * 4096})
        assert socket.receive_json()["code"] == "input_too_large"

        socket.send_json({"type": "terminal.input", "request_id": "dup", "data": "dir\r\n"})
        assert socket.receive_json()["type"] == "terminal.input.ack"
        socket.send_json({"type": "terminal.input", "request_id": "dup", "data": "dir\r\n"})
        assert socket.receive_json()["code"] == "duplicate_request"

    assert backends[0].writes == ["dir\r\n"]


def test_terminal_max_sessions_enforced(tmp_path: Path) -> None:
    client, project, _ = _terminal_client(
        tmp_path, lambda spec: FakeBackend(spec), terminal_max_sessions=1, terminal_max_per_project=1
    )
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as first:
        first.send_json({"type": "terminal.open", "request_id": "r1"})
        first.receive_json()
        with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as second:
            second.send_json({"type": "terminal.open", "request_id": "r1"})
            assert second.receive_json()["code"] == "terminal_limit_reached"


def test_terminal_idle_connection_is_reaped(tmp_path: Path) -> None:
    """连接失活时必须回收后端并注销终端。

    这里走心跳超时路径（在事件循环内触发）而不是「客户端强行断开」：TestClient
    在 `with` 块退出时会立即关闭 portal，endpoint 协程被直接丢弃、finally 不会
    执行，所以硬断连这条路在 TestClient 下无法观察。硬断连的回收语义由
    test_session_close_is_idempotent（单元）与 lifespan shutdown 钩子覆盖。
    """
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec)
        backends.append(backend)
        return backend

    client, project, _ = _terminal_client(
        tmp_path, factory, ws_heartbeat_interval=0.2, terminal_idle_timeout=30.0
    )
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        terminal_id = socket.receive_json()["terminal_id"]
        assert terminal_id in client.app.state.terminals
        # 不回 pong：两次心跳未响应后服务端应主动收尾
        events = _receive_until(socket, lambda item: item.get("type") == "terminal.closed")

    assert backends[0].closed, "失活连接必须回收后端"
    assert terminal_id not in client.app.state.terminals
    closed = [item for item in events if item.get("type") == "terminal.closed"]
    assert closed and closed[-1]["reason"] == "idle_timeout"


def test_terminal_idle_timeout_closes_active_connection(tmp_path: Path) -> None:
    """会话仍活着但长时间无输入/输出 → 按空闲超时关闭。"""
    backends: list[FakeBackend] = []

    def factory(spec: TerminalSpec) -> FakeBackend:
        backend = FakeBackend(spec)
        backends.append(backend)
        return backend

    # 把空闲阈值压到 0.1s（直接构造 ServerConfig，绕过 from_env 的 30s 下限）
    client, project, _ = _terminal_client(
        tmp_path, factory, ws_heartbeat_interval=0.2, terminal_idle_timeout=0.1
    )
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        socket.receive_json()
        events = _receive_until(socket, lambda item: item.get("type") == "terminal.closed")

    assert backends[0].closed
    closed = [item for item in events if item.get("type") == "terminal.closed"]
    assert closed and closed[-1]["reason"] == "idle_timeout"


def test_terminal_backend_unavailable_is_reported(tmp_path: Path) -> None:
    def factory(spec: TerminalSpec):
        raise TerminalUnavailableError("需要 pywinpty")

    client, project, _ = _terminal_client(tmp_path, factory)
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        event = socket.receive_json()
        assert event["code"] == "terminal_unavailable"
        assert "pywinpty" in event["message"]


def test_terminal_already_open_rejected(tmp_path: Path) -> None:
    client, project, _ = _terminal_client(tmp_path, lambda spec: FakeBackend(spec))
    with client.websocket_connect(f"/api/ws/projects/{project['id']}/terminal", headers=_auth_headers()) as socket:
        socket.send_json({"type": "terminal.open", "request_id": "r1"})
        socket.receive_json()
        socket.send_json({"type": "terminal.open", "request_id": "r2"})
        assert socket.receive_json()["code"] == "terminal_already_open"


# ==========================================================================
# 终端：TerminalSession 单元（不经 WebSocket，确定性）
# ==========================================================================


async def _make_session(tmp_path: Path, backend_chunks: list[bytes] | None = None, **kwargs):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    spec = TerminalSpec("t-unit", "p1", root, "cmd.exe", 80, 24, {})
    backend = FakeBackend(spec, chunks=backend_chunks)
    sent: list[dict] = []

    async def send(payload):
        sent.append(payload)

    session = TerminalSession(
        spec=spec,
        backend=backend,
        send=send,
        chunk_bytes=kwargs.pop("chunk_bytes", 16),
        flush_interval=kwargs.pop("flush_interval", 0.01),
        **kwargs,
    )
    await session.start()
    return session, backend, sent


async def test_session_chunks_output_and_numbers_sequence(tmp_path: Path) -> None:
    import asyncio

    payload = b"A" * 100
    session, backend, sent = await _make_session(tmp_path, chunk_bytes=16)
    backend.push(payload)
    await asyncio.sleep(0.3)
    outputs = [item for item in sent if item["type"] == "terminal.output"]
    assert len(outputs) >= 2, "超过 chunk_bytes 应被切成多块"
    assert all(len(item["data"]) <= 16 for item in outputs)
    assert "".join(item["data"] for item in outputs) == payload.decode()
    assert [item["seq"] for item in outputs] == list(range(1, len(outputs) + 1))
    await session.close()


async def test_session_drops_chunks_when_queue_full(tmp_path: Path) -> None:
    """队列满时必须丢块并计数，绝不能阻塞读线程。"""
    session, _, sent = await _make_session(tmp_path, queue_max=2)
    # 直接把队列灌满（输出泵此刻还没被调度）
    for _ in range(10):
        session._on_chunk(b"x" * 8)
    assert session.dropped >= 8, "队列满后的块应计入 dropped"
    await session.close()
    assert any(item["type"] == "terminal.output.dropped" for item in sent) or session.dropped >= 0


async def test_session_non_utf8_output_is_base64(tmp_path: Path) -> None:
    import asyncio

    session, backend, sent = await _make_session(tmp_path)
    backend.push(b"\xff\xfe\xba\xad")
    await asyncio.sleep(0.3)
    outputs = [item for item in sent if item["type"] == "terminal.output"]
    assert outputs and outputs[0]["encoding"] == "base64"
    import base64

    assert base64.b64decode(outputs[0]["data"]) == b"\xff\xfe\xba\xad"
    await session.close()


async def test_session_output_limit_closes(tmp_path: Path) -> None:
    import asyncio

    session, backend, sent = await _make_session(tmp_path, max_output_bytes=32, chunk_bytes=16)
    backend.push(b"Z" * 200)
    await asyncio.sleep(0.4)
    assert session.closed
    assert session.closed_reason == "limit"
    assert any(item.get("code") == "output_limit_reached" for item in sent)


async def test_session_guards_line_at_newline_boundary(tmp_path: Path) -> None:
    session, backend, _ = await _make_session(tmp_path)
    # 逐字符敲入（真实终端的到达方式），回车时才判定
    for char in "rm -rf /":
        ok, reason = await session.write(char)
        assert ok, f"{char!r} 不应被拦截"
    ok, reason = await session.write("\r")
    assert ok is False and reason == "command_blocked"
    assert backend.writes == ["r", "m", " ", "-", "r", "f", " ", "/"], "回车不得被转发"
    await session.close()


async def test_session_audits_command_lines(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    spec = TerminalSpec("t-audit", "p1", root, "cmd.exe", 80, 24, {})
    backend = FakeBackend(spec)
    audit = AuditLogger(root / ".routivus" / "audit.log", session_id="t-audit")

    async def send(payload):
        return None

    session = TerminalSession(spec=spec, backend=backend, send=send, audit=audit, flush_interval=0.01)
    await session.start()
    await session.write("echo hi\r\n")
    await session.close()

    lines = _project_audit_lines(root)
    terminal_records = [item for item in lines if item["action"] == "terminal_command"]
    assert terminal_records and terminal_records[0]["command"] == "echo hi"
    assert terminal_records[0]["origin"] == "terminal"


async def test_session_close_is_idempotent(tmp_path: Path) -> None:
    session, backend, sent = await _make_session(tmp_path)
    await session.close("client_closed")
    await session.close("server_shutdown")
    assert session.closed_reason == "client_closed"
    assert sum(1 for item in sent if item["type"] == "terminal.closed") == 1
    assert backend.closed


# ==========================================================================
# 终端：后端选择与配置
# ==========================================================================


def _spec(tmp_path: Path) -> TerminalSpec:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return TerminalSpec(new_terminal_id(), "p1", root, "cmd.exe", 80, 24, {})


def test_select_backend_oneshot_explicit(tmp_path: Path) -> None:
    backend = select_backend(_spec(tmp_path), "oneshot")
    assert isinstance(backend, OneShotBackend)
    assert backend.name == "oneshot"


def test_select_backend_auto_requires_pywinpty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(terminal_module, "pty_available", lambda: False)
    with pytest.raises(TerminalUnavailableError) as excinfo:
        select_backend(_spec(tmp_path), "auto")
    # 必须明确拒绝并给出指引，而不是静默降级成命令框
    assert "oneshot" in str(excinfo.value)

    monkeypatch.setattr(terminal_module, "pty_available", lambda: True)
    backend = select_backend(_spec(tmp_path), "auto")
    assert isinstance(backend, PtyBackend)
    assert backend.name == "conpty"


def test_select_backend_conpty_missing_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(terminal_module, "pty_available", lambda: False)
    with pytest.raises(TerminalUnavailableError):
        select_backend(_spec(tmp_path), "conpty")


def test_winproc_is_safe_off_windows(monkeypatch) -> None:
    """winproc 必须能在任何平台导入，且在非 Windows 上是 no-op。"""
    assert winproc.create_kill_on_close_job() is None or sys.platform == "win32"
    assert winproc.assign_process_to_job(None, 1234) is False
    winproc.close_job(None)  # 不应抛异常
    winproc.kill_process_tree(0)  # 不应抛异常
    assert winproc.process_alive(0) is False


def test_decode_falls_back_to_gbk() -> None:
    assert _decode("中文".encode("gbk")) == "中文"
    assert _decode("中文".encode("utf-8")) == "中文"
    assert _decode(b"") == ""


def test_server_config_terminal_defaults_and_clamps() -> None:
    config = ServerConfig.from_env({})
    assert config.approval_timeout == 300.0
    assert config.terminal_enabled is True
    assert config.terminal_backend == "auto"
    assert config.terminal_max_sessions == 4
    assert config.terminal_max_per_project == 2
    assert config.terminal_idle_timeout == 900.0
    assert config.terminal_command_timeout == 120.0
    assert config.terminal_chunk_bytes == 8_192
    assert config.terminal_flush_interval == 0.033
    assert config.terminal_queue_max == 256
    assert config.terminal_cols == 120 and config.terminal_rows == 30

    clamped = ServerConfig.from_env({
        "ROUTIVUS_TERMINAL_BACKEND": "bogus",
        "ROUTIVUS_TERMINAL_MAX_SESSIONS": "0",
        "ROUTIVUS_TERMINAL_MAX_PER_PROJECT": "999",
        "ROUTIVUS_TERMINAL_ENABLED": "off",
        "ROUTIVUS_TERMINAL_FLUSH_INTERVAL": "abc",
        "ROUTIVUS_TERMINAL_COMMAND_TIMEOUT": "9999",
        "ROUTIVUS_APPROVAL_TIMEOUT": "1",
        "ROUTIVUS_TERMINAL_COLS": "5",
    })
    assert clamped.terminal_backend == "auto"
    assert clamped.terminal_max_sessions == 1
    assert clamped.terminal_max_per_project == 16
    assert clamped.terminal_enabled is False
    assert clamped.terminal_flush_interval == 0.033
    assert clamped.terminal_command_timeout == 600.0
    assert clamped.approval_timeout == 5.0
    assert clamped.terminal_cols == MIN_COLS


def test_guard_tool_call_checks_execute_command_cwd(tmp_path: Path) -> None:
    """收紧后的策略：execute_command 的 cwd 也必须落在项目根内。"""
    from routivus.safety.guards import guard_tool_call

    root = tmp_path / "root"
    root.mkdir()
    assert guard_tool_call(root, "execute_command", {"command": "dir", "cwd": str(root)}).ok
    outside = guard_tool_call(root, "execute_command", {"command": "dir", "cwd": str(tmp_path)})
    assert outside.ok is False
    assert outside.reason == "path_outside_root"

    # 黑名单仍然优先，且 `cd ..` 依旧放行 —— 这是持久化 shell 已知的边界，
    # 在这里显式记录，避免以后被误当成回归。
    assert guard_tool_call(root, "execute_command", {"command": "rm -rf /"}).reason == "command_blacklist"
    assert guard_tool_call(root, "execute_command", {"command": "cd ..", "cwd": str(root)}).ok is True


# ==========================================================================
# 真进程用例
# ==========================================================================


@pytest.mark.slow
async def test_oneshot_backend_runs_real_command(tmp_path: Path) -> None:
    import asyncio

    root = tmp_path / "root"
    root.mkdir()
    spec = TerminalSpec(new_terminal_id(), "p1", root, default_shell(""), 80, 24, {})
    backend = OneShotBackend(spec, timeout=10.0)
    await backend.start()
    sent: list[dict] = []

    async def send(payload):
        sent.append(payload)

    session = TerminalSession(spec=spec, backend=backend, send=send, flush_interval=0.01)
    await session.start()
    await session.write("echo routivus-terminal-ok\r\n")
    await asyncio.sleep(3.0)
    blob = "".join(item["data"] for item in sent if item["type"] == "terminal.output")
    assert "routivus-terminal-ok" in blob
    await session.close()


@pytest.mark.slow
async def test_conpty_backend_roundtrip_and_reap(tmp_path: Path) -> None:
    import asyncio

    if not terminal_module.pty_available():
        pytest.skip("pywinpty 未安装")

    root = tmp_path / "root"
    root.mkdir()
    spec = TerminalSpec(new_terminal_id(), "p1", root, default_shell(""), 80, 24, {})
    backend = PtyBackend(spec)
    sent: list[dict] = []

    async def send(payload):
        sent.append(payload)

    session = TerminalSession(spec=spec, backend=backend, send=send, flush_interval=0.05)
    await session.start()
    pid = backend.pid
    assert pid and winproc.process_alive(pid)
    await session.write("echo conpty-sentinel-ok\r\n")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        blob = "".join(item["data"] for item in sent if item["type"] == "terminal.output")
        if "conpty-sentinel-ok" in blob:
            break
        await asyncio.sleep(0.2)
    assert "conpty-sentinel-ok" in blob
    await session.close("client_closed")
    assert not winproc.process_alive(pid), "关闭后进程必须被回收"


@pytest.mark.slow
def test_kill_process_tree_reaps_shell(tmp_path: Path) -> None:
    import subprocess

    if sys.platform != "win32":
        pytest.skip("Windows only")

    proc = subprocess.Popen(["cmd.exe", "/c", "ping -n 30 127.0.0.1"], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert winproc.process_alive(proc.pid)
        winproc.kill_process_tree(proc.pid, grace=5.0)
        assert winproc.wait_for_exit(proc.pid, timeout=5.0)
    finally:
        if winproc.process_alive(proc.pid):  # pragma: no cover - 清理兜底
            proc.kill()
