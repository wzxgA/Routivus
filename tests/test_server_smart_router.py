"""会话内 SmartRouter 接线的协议级测试。

路由算法本身由 `tests/test_router.py`、`test_adaptive_stepd.py` 覆盖；本文件验证
的是**服务端接线**：开关门禁、路由结果下行、模型切换调用、失败降级，以及
「手动切模型即关闭路由」的手动优先语义。

为避免触碰真实用户目录、加载 ML 产物，这里替换掉校准 / 自学习 / ML / 语义编码器
与反馈落盘，并把 `routivus.router.route` 换成固定结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from routivus.agent.react import AgentEvent
from routivus.config.settings import Settings
from routivus.router import RouteResult
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore


# ==========================================================================
# 替身
# ==========================================================================


class _StubFeedback:
    """反馈采集器替身：只记账，不落盘。"""

    instances: list["_StubFeedback"] = []

    def __init__(self, session: str = "", **kwargs: Any) -> None:
        self.session = session
        self.captured: list[tuple[Any, str]] = []
        self.flushes = 0
        _StubFeedback.instances.append(self)

    def capture(self, signal: Any, **kwargs: Any) -> None:
        self.captured.append((signal, str(kwargs.get("model_tier") or "")))

    def flush(self, force: bool = False) -> int:
        self.flushes += 1
        return 0


class _StubMLRouter:
    """ML 精判替身：永远不可用（等于产物缺失时的静默回落）。"""

    available = False

    def __init__(self, **kwargs: Any) -> None:
        pass


class _RouterAgent:
    """够用的假 agent：路由需要 settings 与 config_manager。

    `llm` / `tools` 只为让 `/plan` 轮通过服务端的依赖检查（本文件不验计划执行）。
    """

    def __init__(self, settings: Settings, manager: Any) -> None:
        self.settings = settings
        self.config_manager = manager
        self.llm = object()
        self.tools = object()
        self.approval_policy = None
        self.ask_requester = None
        self.seen: list[str] = []

    async def run(self, content: str) -> AsyncIterator[AgentEvent]:
        self.seen.append(content)
        yield AgentEvent(kind="content", text=f"回：{content}")
        yield AgentEvent(kind="done")


class _FakeManager:
    def __init__(self, tiers: dict[str, Any] | None = None) -> None:
        self.tiers = tiers or {}

    def smart_router_config(self) -> dict[str, Any]:
        return {"enabled": True, "tiers": self.tiers}


class _EmptyPlanExecutor:
    """`/plan` 回合用的空执行器：只为验证「计划轮不路由」。"""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def run(self, goal: str) -> AsyncIterator[Any]:
        from routivus.agent.plan import PlanEvent

        yield PlanEvent(kind="plan_failed", message="stub")

    async def resume(self, instruction: str = "") -> AsyncIterator[Any]:
        from routivus.agent.plan import PlanEvent

        yield PlanEvent(kind="plan_failed", message="stub")


def _route_result(
    *,
    tier: str = "Superior",
    provider: str = "base",
    model: str = "m-superior",
    configured: bool = True,
) -> RouteResult:
    return RouteResult(
        tier=tier,
        tier_idx=2,
        provider=provider,
        model=model,
        configured=configured,
        confidence=0.72,
        score=3.1,
        hard_rule=False,
        features={"len_chars": 12},
    )


@pytest.fixture(autouse=True)
def _stub_adaptive(monkeypatch: pytest.MonkeyPatch) -> None:
    import routivus.server.routing as routing_module

    _StubFeedback.instances = []
    # 重资产是进程级单例：跨用例必须清掉，否则后面的用例会复用前一个用例的替身
    routing_module.reset_shared_assets()
    monkeypatch.setattr("routivus.adaptive.calibrate.recalibrate", lambda: None)
    monkeypatch.setattr("routivus.adaptive.learned_rules.re_learn", lambda: None)
    monkeypatch.setattr("routivus.router.ml_router.MLRouter", _StubMLRouter)
    monkeypatch.setattr("routivus.router.semantic.load_semantic_encoder", lambda: None)
    monkeypatch.setattr("routivus.adaptive.feedback.FeedbackRecorder", _StubFeedback)
    monkeypatch.setattr("routivus.agent.plan.PlanExecutor", _EmptyPlanExecutor)


def _install_route(monkeypatch: pytest.MonkeyPatch, result: RouteResult) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_route(text: str, **kwargs: Any) -> RouteResult:
        calls.append({"text": text, **kwargs})
        return result

    monkeypatch.setattr("routivus.router.route", fake_route)
    return calls


def _install_attach(monkeypatch: pytest.MonkeyPatch, *, error: str = "") -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_attach(settings: Any, manager: Any, agent: Any, provider: str, model: str) -> str | None:
        calls.append((provider, model))
        if error:
            return error
        settings.provider, settings.model = provider, model
        return None

    monkeypatch.setattr("routivus.service.commands._attach_model", fake_attach)
    return calls


# ==========================================================================
# 测试脚手架
# ==========================================================================


def _client(
    tmp_path: Path,
    *,
    settings: Settings,
    manager: Any | None = None,
) -> tuple[TestClient, _RouterAgent]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    agent = _RouterAgent(settings, manager if manager is not None else _FakeManager())
    client = TestClient(
        create_app(registry=registry, store=store, config=config, agent_factory=lambda project, session: agent)
    )
    return client, agent


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir()
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _session(client: TestClient, project: dict) -> dict:
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201
    return response.json()


def _ws(project: dict, session: dict) -> str:
    return f"/api/ws/projects/{project['id']}/sessions/{session['id']}"


def _receive_until(socket: Any, predicate: Any, limit: int = 60) -> list[dict]:
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


def _finished(event: dict) -> bool:
    """轮次结束（completed / failed / cancelled）。

    等待时用它而不是 `_completed`：轮次失败时若只认 completed，会一直等到心跳
    超时，把回归变成"卡 60s 后才失败"。
    """
    return event.get("type") == "session.status" and event.get("data", {}).get("status") in {
        "completed",
        "failed",
        "cancelled",
    }


def _routers(events: list[dict]) -> list[dict]:
    return [item["data"] for item in events if item.get("type") == "router.updated"]


def _chat(client: TestClient, project: dict, session: dict, content: str = "看看配置", request_id: str = "r1") -> list[dict]:
    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": request_id, "content": content})
        return _receive_until(socket, _finished)


# ==========================================================================
# 开关门禁
# ==========================================================================


def test_switch_off_does_not_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_route(monkeypatch, _route_result())
    attach = _install_attach(monkeypatch)
    client, _ = _client(tmp_path, settings=Settings(smart_router_enabled=False))
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session)

    assert calls == [], "开关关闭时不应调用路由"
    assert attach == []
    assert _routers(events) == []
    # 会话快照里的 router 也是关闭态
    with client.websocket_connect(_ws(project, session)) as socket:
        snapshot = socket.receive_json()
    assert snapshot["data"]["router"]["enabled"] is False


def test_switch_on_routes_switches_model_and_reports_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_route(monkeypatch, _route_result())
    attach = _install_attach(monkeypatch)
    settings = Settings(provider="base", model="m-basic", smart_router_enabled=True)
    client, agent = _client(tmp_path, settings=settings)
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session, "重构检索层并补回归测试")

    assert len(calls) == 1 and calls[0]["text"] == "重构检索层并补回归测试"
    assert calls[0]["fallback_provider"] == "base" and calls[0]["fallback_model"] == "m-basic"
    assert attach == [("base", "m-superior")], "应按路由结果换模型"
    assert (settings.provider, settings.model) == ("base", "m-superior")
    # 模型切换发生在实际执行之前
    assert agent.seen == ["重构检索层并补回归测试"]

    payloads = _routers(events)
    assert len(payloads) == 1
    assert payloads[0]["tier"] == "Superior"
    assert payloads[0]["model"] == "m-superior"
    assert payloads[0]["configured"] is True
    assert "error" not in payloads[0]

    # 反馈信号采集并落盘各一次
    recorder = _StubFeedback.instances[-1]
    assert recorder.flushes == 1
    assert isinstance(recorder.captured, list)


def test_routed_tier_is_kept_in_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_route(monkeypatch, _route_result(tier="Ultimate", model="m-ultimate"))
    _install_attach(monkeypatch)
    client, _ = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)
    _chat(client, project, session)

    with client.websocket_connect(_ws(project, session)) as socket:
        snapshot = socket.receive_json()

    router = snapshot["data"]["router"]
    assert router["enabled"] is True
    assert router["tier"] == "Ultimate"
    assert router["model"] == "m-ultimate"


# ==========================================================================
# 失败与降级
# ==========================================================================


def test_attach_failure_is_reported_and_does_not_advance_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_route(monkeypatch, _route_result())
    _install_attach(monkeypatch, error="缺少 base 的 api_key 配置")
    client, agent = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session)

    payloads = _routers(events)
    assert payloads and "api_key" in payloads[0]["error"]
    # 切换失败：不推进防降级上下文，本轮沿用原模型
    router = client.app.state.session_routers[session["id"]]
    assert router.prev_tier is None
    assert agent.seen == ["看看配置"], "换模型失败不应阻断对话"
    assert _completed(events[-1])


def test_router_init_failure_degrades_to_no_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_route(monkeypatch, _route_result())
    calls = _install_attach(monkeypatch)

    def boom(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("lightgbm 缺失")

    monkeypatch.setattr("routivus.server.routing.SessionRouter.__init__", boom)
    client, agent = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session)

    assert calls == [], "路由运行态建不起来时不应继续换模型"
    payloads = _routers(events)
    assert payloads and "初始化失败" in payloads[0]["error"]
    assert agent.seen == ["看看配置"], "仍是正常的一轮对话"
    assert _completed(events[-1])
    assert client.app.state.session_routers[session["id"]] is None


def test_route_exception_does_not_break_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str, **kwargs: Any) -> Any:
        raise RuntimeError("特征提取炸了")

    monkeypatch.setattr("routivus.router.route", boom)
    _install_attach(monkeypatch)
    client, agent = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session)

    payloads = _routers(events)
    assert payloads and "路由失败" in payloads[0]["error"]
    assert agent.seen == ["看看配置"]
    assert _completed(events[-1])


def test_route_timeout_degrades_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路由超时只降级为「本轮不换档」，不阻断对话。"""
    import time as time_module

    import routivus.server.routing as routing_module

    monkeypatch.setattr("routivus.server.app.route_timeout_seconds", lambda: 0.2)

    def slow_apply(self: Any, text: str, **kwargs: Any) -> Any:
        time_module.sleep(1.0)
        return _route_result(), ""

    monkeypatch.setattr(routing_module.SessionRouter, "apply", slow_apply)
    client, agent = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    events = _chat(client, project, session)

    payloads = _routers(events)
    assert payloads and "超时" in payloads[0]["error"]
    # 超时后仍照常对话，不换模型
    assert agent.seen == ["看看配置"]
    assert _completed(events[-1])


def test_slow_routing_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：路由是同步重活，绝不能在事件循环线程里跑。

    之前 `SessionRouter.__init__` 直接加载 20+ MB 模型，首轮会把整个 uvicorn
    事件循环占死 —— 表现为"一对话就卡住"，连 /healthz 都不响应。这里让 apply
    故意慢 1s，同时在轮次进行中打一个 /healthz：若事件循环被占死，它会等满。
    """
    import time as time_module

    import routivus.server.routing as routing_module

    started = time_module.perf_counter()

    def slow_apply(self: Any, text: str, **kwargs: Any) -> Any:
        time_module.sleep(1.0)
        return _route_result(), ""

    monkeypatch.setattr(routing_module.SessionRouter, "apply", slow_apply)
    client, _ = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "看看配置"})
        _receive_until(socket, lambda item: item.get("data", {}).get("status") == "running")
        probe = time_module.perf_counter()
        response = client.get("/healthz")
        probe_seconds = time_module.perf_counter() - probe
        events = _receive_until(socket, _finished)

    assert response.status_code == 200
    assert probe_seconds < 0.6, f"事件循环被路由阻塞了 {probe_seconds:.2f}s"
    assert time_module.perf_counter() - started >= 1.0, "apply 确实慢过"
    assert _routers(events), "慢路由最终仍应回报档位"


# ==========================================================================
# 门禁：计划 / 团队轮不路由
# ==========================================================================


def test_plan_turn_does_not_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_route(monkeypatch, _route_result())
    attach = _install_attach(monkeypatch)
    client, _ = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=True)
    )
    project = _project(client)
    session = _session(client, project)

    with client.websocket_connect(_ws(project, session)) as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "request_id": "r1", "content": "/plan 重构检索层"})
        events = _receive_until(socket, _finished)

    assert calls == [], "/plan 不参与智能路由（与 TUI 一致）"
    assert attach == []
    assert _routers(events) == []
    assert [item["data"]["kind"] for item in events if item.get("type") == "plan.updated"] == ["plan_failed"]
    assert _completed(events[-1])


# ==========================================================================
# 手动优先：显式切模型即关闭智能路由
# ==========================================================================


def test_manual_model_switch_disables_smart_router(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, settings=Settings())
    response = client.post(
        "/api/config/providers",
        json={"name": "base", "api_base": "https://gateway.example.com/v1", "default_model": "m0"},
    )
    assert response.status_code == 201

    # 配置档位会自动开启总闸
    put = client.put("/api/config/tiers/Basic", json={"provider": "base", "model": "m-basic"})
    assert put.status_code == 200
    assert put.json()["smart_router_enabled"] is True

    switched = client.post("/api/config/active", json={"provider": "base", "model": "m-manual"})
    assert switched.status_code == 200
    assert switched.json()["smart_router_enabled"] is False, "手动切模型应关闭智能路由"
    assert client.get("/api/config").json()["smart_router_enabled"] is False


# ==========================================================================
# 开关即时生效 + 重资产预热
# ==========================================================================


def test_enable_applies_to_existing_session_and_prewarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：开关必须对已存在的会话即时生效，并在开启时后台预热。"""
    import routivus.server.app as app_module

    calls = _install_route(monkeypatch, _route_result())
    _install_attach(monkeypatch)
    prewarm_calls: list[int] = []
    monkeypatch.setattr(
        app_module, "prewarm_shared_assets", lambda *args, **kwargs: prewarm_calls.append(1) or True
    )

    client, _ = _client(
        tmp_path, settings=Settings(provider="base", model="m0", smart_router_enabled=False)
    )
    project = _project(client)
    session = _session(client, project)

    # 开启前：旧会话不路由、不预热
    _chat(client, project, session, "先聊一句", request_id="r0")
    assert calls == [], "开启前不应调用路由"
    assert prewarm_calls == [], "未开启时不应加载重资产"

    # 通过配置接口开启（会话不重建）
    response = client.post("/api/config/smart-router", json={"enabled": True})
    assert response.status_code == 200 and response.json()["smart_router_enabled"] is True
    assert prewarm_calls == [1], "点击开启应在后台预热重资产"

    # 同一个旧会话立即可路由
    events = _chat(client, project, session, "再聊一句", request_id="r1")
    assert len(calls) == 1 and calls[0]["text"] == "再聊一句"
    assert _routers(events), "开启后已存在的会话也应产出路由结果"


def test_startup_prewarms_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：服务启动时仅在开关开启的情况下后台预热重资产。"""
    import routivus.server.app as app_module
    from routivus.config.manager import ConfigManager

    prewarm_calls: list[int] = []
    monkeypatch.setattr(
        app_module, "prewarm_shared_assets", lambda *args, **kwargs: prewarm_calls.append(1) or True
    )

    def boot(tag: str, enabled: bool) -> None:
        user_dir = tmp_path / f"userdata_{tag}"
        workspace = tmp_path / f"workspace_{tag}"
        workspace.mkdir()
        database = tmp_path / f"{tag}.sqlite3"
        projects_file = tmp_path / f"{tag}-projects.json"
        if enabled:
            ConfigManager(
                user_dir=user_dir, project_dir=user_dir / "np", load_env=False
            ).set_smart_router_enabled(True)
        app = create_app(
            registry=ProjectRegistry(projects_file, [workspace]),
            store=WorkspaceStore(database),
            config=ServerConfig(
                projects_file=projects_file,
                workspace_roots=(workspace,),
                user_dir=user_dir,
                database_path=database,
            ),
            agent_factory=lambda project, session: None,
        )
        with TestClient(app):
            pass

    boot("off", enabled=False)
    assert prewarm_calls == [], "开关关闭时启动不应加载"
    boot("on", enabled=True)
    assert prewarm_calls == [1], "开关开启时启动应预热"


def test_blocking_prewarm_runs_on_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：启动预热必须同步在调用线程（事件循环启动前的主线程）完成。

    后台线程里做首次重型 import 会与事件循环首次创建 AnyIO worker 线程竞态
    死锁。这里用替身证明 ``blocking=True`` 不会另起线程。
    """
    import threading

    import routivus.server.routing as routing_module

    seen: dict[str, str] = {}

    def fake_worker() -> None:
        seen["thread"] = threading.current_thread().name

    monkeypatch.setattr(routing_module, "_prewarm_worker", fake_worker)

    assert routing_module.prewarm_shared_assets(blocking=True) is True
    assert seen["thread"] == threading.current_thread().name


def test_prewarm_if_enabled_follows_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：事件循环前的预热只在开关开启时真正加载。"""
    from routivus.config.manager import ConfigManager

    import routivus.server.routing as routing_module

    user_dir = tmp_path / "userdata"
    called: list[bool] = []
    monkeypatch.setattr(
        routing_module,
        "prewarm_shared_assets",
        lambda **kwargs: called.append(bool(kwargs.get("blocking"))) or True,
    )

    assert routing_module.prewarm_shared_assets_if_enabled(user_dir) is False
    assert called == [], "开关关闭时不应加载重资产"

    ConfigManager(
        user_dir=user_dir, project_dir=user_dir / "np", load_env=False
    ).set_smart_router_enabled(True)
    assert routing_module.prewarm_shared_assets_if_enabled(user_dir) is True
    assert called == [True], "开关开启时应同步加载"
