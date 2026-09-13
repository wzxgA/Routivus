"""模型上下文上限（窗口 + 最大输出）的解析、写入、下发与暴露。

覆盖 plans/enhancement/06：

- 解析链：env > 模型覆盖 > provider 默认 > 兜底（窗口）；模型覆盖 > provider > 0（输出）
- 字段名白名单：拼错不静默生效，回落默认
- REST 写入与回显、模型覆盖整表覆盖语义、非法值 422
- 请求体下发条件（**未配不带键**、字段名可换、空字段名不发送）
- 网关拒绝时的可操作提示，且**不重试**（自动降级会把功能静默关掉）
- 预算的 response_reserve 改用配置值
- 会话快照 / `context.updated` 的传播
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from routivus.config.manager import ConfigManager
from routivus.config.providers import Provider
from routivus.config.settings import load_settings
from routivus.llm.client import LlmError
from routivus.llm.openai_compat import OpenAICompatClient, _output_limit_hint
from routivus.llm.types import Message
from routivus.memory.context import ConversationContext
from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.storage import WorkspaceStore

API_URL = "https://api.test/v1/chat/completions"


def sse_response(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=body.encode("utf-8"), headers={"Content-Type": "text/event-stream"}
    )


async def collect(client: OpenAICompatClient, messages: list[Message]):
    return [event async for event in client.stream_chat(messages)]


# ==========================================================================
# 配置层：解析链
# ==========================================================================


def _manager(tmp_path: Path, config: dict, env: dict[str, str] | None = None) -> ConfigManager:
    user_dir = tmp_path / "ud"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    return ConfigManager(
        user_dir=user_dir,
        project_dir=tmp_path / "proj" / ".routivus",
        env=env or {},
        env_file=None,
        load_env=False,
    )


def _provider_config(**capability: Any) -> dict:
    provider: dict[str, Any] = {
        "api_base": "https://api.test/v1",
        "default_model": "small",
        "api_key": "sk-test",
    }
    provider.update(capability)
    return {"active_provider": "p", "active_model": "small", "providers": {"p": provider}}


class TestWindowResolution:
    def test_provider_default_applies_to_every_model(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, _provider_config(context_window=64000))
        provider = manager.resolve_provider("p")
        assert manager.resolve_window(provider, "small") == 64000
        assert manager.resolve_window(provider, "big") == 64000
        assert manager.resolve_window_detail(provider, "big") == (64000, "provider")

    def test_model_override_wins_over_provider_default(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            _provider_config(context_window=64000, model_limits={"big": {"window": 400000}}),
        )
        provider = manager.resolve_provider("p")
        assert manager.resolve_window(provider, "big") == 400000
        assert manager.resolve_window(provider, "small") == 64000
        assert manager.resolve_window_detail(provider, "big") == (400000, "model")

    def test_env_overrides_everything(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            _provider_config(context_window=64000, model_limits={"big": {"window": 400000}}),
            env={"ROUTIVUS_CONTEXT_WINDOW": "16000"},
        )
        provider = manager.resolve_provider("p")
        assert manager.resolve_window(provider, "big") == 16000
        assert manager.resolve_window_detail(provider, "big") == (16000, "env")

    def test_zero_window_falls_back_to_default(self, tmp_path: Path) -> None:
        """手写 / 历史配置可能没有窗口：回落默认值而不是按 0 算预算。"""
        manager = _manager(tmp_path, {"providers": {}})
        provider = Provider(
            name="x", display_name="x", api_base="https://api.test/v1", default_model="m"
        )
        assert manager.resolve_window_detail(provider, "m") == (128_000, "default")

    def test_active_snapshot_resolves_per_active_model(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            _provider_config(context_window=64000, model_limits={"big": {"window": 400000}}),
        )
        assert manager.active().context_window == 64000  # active_model = small
        manager.set_active("p", "big")
        assert manager.active().context_window == 400000


class TestOutputLimitResolution:
    def test_unset_means_zero(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path, _provider_config(context_window=128000))
        provider = manager.resolve_provider("p")
        assert manager.resolve_output_limit(provider, "small") == 0
        assert manager.resolve_output_field(provider) == "max_tokens"

    def test_model_override_wins_over_provider_default(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            _provider_config(
                context_window=128000,
                max_output_tokens=4096,
                model_limits={"big": {"max_output": 8192}},
            ),
        )
        provider = manager.resolve_provider("p")
        assert manager.resolve_output_limit(provider, "small") == 4096
        assert manager.resolve_output_limit(provider, "big") == 8192

    def test_value_is_clamped_to_window(self, tmp_path: Path) -> None:
        """prompt + max_tokens > window 是必然失败的请求，配置层面就夹住。"""
        manager = _manager(tmp_path, _provider_config(context_window=8000, max_output_tokens=32000))
        provider = manager.resolve_provider("p")
        assert manager.resolve_output_limit(provider, "small") == 8000

    def test_field_name_whitelist(self, tmp_path: Path) -> None:
        typed = _manager(tmp_path / "typed", _provider_config(max_tokens_field="max_completion_tokens"))
        assert typed.resolve_output_field(typed.resolve_provider("p")) == "max_completion_tokens"

        # 拼错的字段名回落默认：静默不生效比报错更难查
        broken = _manager(tmp_path / "broken", _provider_config(max_tokens_field="max_token"))
        assert broken.resolve_output_field(broken.resolve_provider("p")) == "max_tokens"

        never = _manager(tmp_path / "never", _provider_config(max_tokens_field=""))
        assert never.resolve_output_field(never.resolve_provider("p")) == ""


# ==========================================================================
# 请求体：默认不带键，配了才发，字段名可选
# ==========================================================================


class TestRequestPayload:
    @respx.mock
    async def test_unset_sends_no_output_cap_key(self, settings) -> None:
        """默认绝不能带 max_tokens: 0 —— 有的网关会理解成「最多输出 0 token」。"""
        route = respx.post(API_URL).mock(
            return_value=sse_response({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )
        client = OpenAICompatClient(settings.api_base, settings.api_key, settings.model)
        await collect(client, [Message(role="user", content="hi")])

        body = json.loads(route.calls[0].request.content)
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body

    @respx.mock
    async def test_configured_limit_uses_the_chosen_field(self, settings) -> None:
        route = respx.post(API_URL).mock(
            return_value=sse_response({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )
        client = OpenAICompatClient(
            settings.api_base, settings.api_key, settings.model,
            max_tokens=512, max_tokens_field="max_completion_tokens",
        )
        await collect(client, [Message(role="user", content="hi")])

        body = json.loads(route.calls[0].request.content)
        assert body["max_completion_tokens"] == 512
        assert "max_tokens" not in body

    @respx.mock
    async def test_empty_field_never_sends(self, settings) -> None:
        route = respx.post(API_URL).mock(
            return_value=sse_response({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )
        client = OpenAICompatClient(
            settings.api_base, settings.api_key, settings.model,
            max_tokens=512, max_tokens_field="",
        )
        await collect(client, [Message(role="user", content="hi")])

        body = json.loads(route.calls[0].request.content)
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body


class TestGatewayHint:
    def test_hint_offers_a_way_to_fix(self) -> None:
        hint = _output_limit_hint(
            400,
            '{"error":{"message":"Unrecognized request argument supplied: max_tokens"}}',
            "max_tokens",
            512,
        )
        assert "max_completion_tokens" in hint
        assert "不发送" in hint

    def test_no_hint_when_no_limit_was_sent(self) -> None:
        assert _output_limit_hint(400, "unknown parameter: max_tokens", "max_tokens", 0) == ""

    def test_no_hint_for_unrelated_errors(self) -> None:
        assert _output_limit_hint(400, "model not found", "max_tokens", 512) == ""

    @respx.mock
    async def test_error_message_carries_hint_without_retrying(self, settings) -> None:
        """只加文案、只发一次：自动删字段重试会让「限制输出」静默失效。"""
        route = respx.post(API_URL).mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": "Unrecognized request argument supplied: max_tokens"}},
                headers={"Content-Type": "application/json"},
            )
        )
        client = OpenAICompatClient(
            settings.api_base, settings.api_key, settings.model,
            max_tokens=512, retry_enabled=True, max_retries=2,
        )
        with pytest.raises(LlmError) as excinfo:
            await collect(client, [Message(role="user", content="hi")])

        assert "max_completion_tokens" in str(excinfo.value)
        assert route.call_count == 1


# ==========================================================================
# 预算：输出预留改用配置值
# ==========================================================================


class TestBudgetUsesConfiguredOutputLimit:
    """输出预留参与输入上限：配了就用配置值，没配才回落 15% 公式。

    `_budget_values()` 返回 (输入上限, 压缩触发点, 摘要预留)——输出预留不直接返回，
    它体现在输入上限里，所以断言看输入上限。
    """

    def _request_limit(self, settings, output: int) -> int:
        settings.context_window = 100_000
        settings.budget_ratio = 0.9
        settings.max_output_tokens = output
        limit, _trigger, _summary = ConversationContext("p", settings)._budget_values()
        return limit

    def test_configured_limit_takes_over_the_formula(self, settings) -> None:
        # 公式：15% = 15000 → 100000 − 15000 − 5000(安全垫) = 80000
        assert self._request_limit(settings, 0) == 80_000
        # 配置 8000 → 100000 − 8000 − 5000 = 87000（预算随实际输出上限收紧）
        assert self._request_limit(settings, 8_000) == 87_000

    def test_oversized_limit_is_capped_at_half_window(self, settings) -> None:
        # 90000 被夹到窗口的 50% = 50000 → 100000 − 50000 − 5000 = 45000
        assert self._request_limit(settings, 90_000) == 45_000

    def test_tiny_limit_leaves_input_to_the_budget_ratio(self, settings) -> None:
        # 输出只要 2000 时，输入侧就回到 budget_ratio 的天花板（90% = 90000）
        assert self._request_limit(settings, 2_000) == 90_000


# ==========================================================================
# REST：写入、回显、校验
# ==========================================================================


def _client(tmp_path: Path, factory: Any = None) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
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
            agent_factory=factory or (lambda project, session: object()),
        )
    )


def _create_provider(client: TestClient, **capability: Any) -> dict:
    payload: dict[str, Any] = {
        "name": "gw",
        "api_base": "https://gw.test/v1",
        "default_model": "m1",
    }
    payload.update(capability)
    response = client.post("/api/config/providers", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestProviderCapabilityApi:
    def test_create_returns_capabilities(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        view = _create_provider(
            client,
            context_window=64000,
            max_output_tokens=4096,
            max_tokens_field="max_completion_tokens",
            model_limits={"m1": {"window": 400000}},
        )
        assert view["context_window"] == 64000
        assert view["max_output_tokens"] == 4096
        assert view["max_tokens_field"] == "max_completion_tokens"
        assert view["model_limits"]["m1"]["window"] == 400000

    def test_snapshot_reflects_active_model_override(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        _create_provider(client, context_window=64000, model_limits={"m2": {"window": 400000}})
        client.post("/api/config/active", json={"provider": "gw", "model": "m2"})

        snapshot = client.get("/api/config").json()
        assert snapshot["active_model"] == "m2"
        assert snapshot["context_window"] == 400000
        assert snapshot["max_output_tokens"] == 0
        assert snapshot["max_tokens_field"] == "max_tokens"

    def test_empty_model_limits_clears_overrides(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        _create_provider(client, context_window=64000, model_limits={"m1": {"window": 400000}})
        client.post("/api/config/active", json={"provider": "gw", "model": "m1"})
        assert client.get("/api/config").json()["context_window"] == 400000

        response = client.patch("/api/config/providers/gw", json={"model_limits": {}})
        assert response.status_code == 200
        assert response.json()["model_limits"] == {}
        assert client.get("/api/config").json()["context_window"] == 64000

    @pytest.mark.parametrize(
        "payload",
        [
            {"context_window": 100},
            {"context_window": 0},
            {"max_output_tokens": -1},
            {"max_tokens_field": "max_token"},
            {"model_limits": {"m1": {"window": 10}}},
            {"model_limits": {"m1": {"nope": 1}}},
        ],
    )
    def test_invalid_capability_is_rejected(self, tmp_path: Path, payload: dict) -> None:
        client = _client(tmp_path)
        _create_provider(client)
        response = client.patch("/api/config/providers/gw", json=payload)
        assert response.status_code == 422


# ==========================================================================
# 传播：会话快照 + /model 命令事件
# ==========================================================================


class _CmdAgent:
    """带真实 ConfigManager 的替身：/model 走真逻辑（与 Web 命令测试同构）。"""

    def __init__(self, manager: ConfigManager) -> None:
        self.llm = object()
        self.tools = object()
        self.settings = load_settings(manager)
        self.config_manager = manager
        self.approval_policy = None
        self.memory_manager = None

    def clear(self) -> None:
        return None


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


class TestSessionContextPropagation:
    def test_snapshot_carries_context_without_any_turn(self, tmp_path: Path) -> None:
        """刚打开会话（agent 还没建）也要看到真数，不能等下一轮对话。"""
        client = _client(tmp_path)
        _create_provider(client, context_window=64000, model_limits={"m2": {"window": 400000}})
        client.post("/api/config/active", json={"provider": "gw", "model": "m2"})
        project = _project(client)
        session = _session(client, project)

        with client.websocket_connect(_ws(project, session)) as socket:
            snapshot = socket.receive_json()

        context = snapshot["data"]["context"]
        assert context["window"] == 400000
        assert context["model"] == "m2"
        assert context["source"] == "model"

    def test_model_command_pushes_context_updated(self, tmp_path: Path) -> None:
        """`/model` 换模型后窗口变化必须实时同步，否则界面分母一直是旧值。"""
        manager = ConfigManager(
            user_dir=tmp_path / "userdata",
            project_dir=tmp_path / "workspace" / ".routivus",
            env={},
            env_file=None,
            load_env=False,
        )
        client = _client(tmp_path, factory=lambda project, session: _CmdAgent(manager))
        _create_provider(client, context_window=64000, model_limits={"m2": {"window": 400000}})
        # `/model <model>` 只能切到 provider 模型列表内的模型；切模型还需要 key
        assert client.post("/api/config/providers/gw/models", json={"model": "m2"}).status_code == 200
        assert client.post("/api/config/providers/gw/key", json={"api_key": "sk-test"}).status_code == 200
        client.post("/api/config/active", json={"provider": "gw", "model": "m1"})
        project = _project(client)
        session = _session(client, project)

        contexts: list[dict] = []
        with client.websocket_connect(_ws(project, session)) as socket:
            socket.receive_json()  # snapshot
            socket.send_json({"type": "user_message", "content": "/model m2", "request_id": "r1"})
            for _ in range(40):
                event = socket.receive_json()
                if event.get("type") == "context.updated":
                    contexts.append(event["data"])
                    break

        assert contexts, "未收到 context.updated"
        assert contexts[0]["window"] == 400000
        assert contexts[0]["model"] == "m2"
        assert contexts[0]["source"] == "model"
