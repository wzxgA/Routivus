from __future__ import annotations

from dataclasses import replace

import pytest

from xg.ask.models import AskField, AskOption, AskRequest
from xg.agent.react import AgentEvent, ReActAgent
from xg.config.manager import ConfigManager
from xg.config.settings import Settings
from xg.llm.client import LlmClient
from xg.llm.types import StreamEvent
from xg.tool.builtin import build_registry
from xg.tui.app import XgTuiApp
from xg.tui.reducer import reduce_agent_event
from xg.llm.types import ToolCall
from xg.tui.state import TuiState
from xg.tui.widgets.ask_panel import AskPanel


class EmptyClient(LlmClient):
    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(kind="done")


def make_request() -> AskRequest:
    return AskRequest(
        id="ask-test-1",
        prompt="先确认改造范围",
        fields=(
            AskField(
                key="scope",
                question="改造范围？",
                options=(
                    AskOption("仅后端", "backend"),
                    AskOption("前后端", "both"),
                ),
                required=True,
            ),
            AskField(key="note", question="补充说明？"),
        ),
    )


def test_ask_panel_advances_fields_and_returns_answers():
    panel = AskPanel()
    panel.update_request(make_request())

    assert panel.display is True
    assert panel.current_field.key == "scope"
    assert panel._buttons[0].display is True
    assert panel._buttons[1].display is True
    assert panel._buttons[2].display is False
    assert panel.select_option(2) is True

    next_field = panel.submit_text("")
    assert next_field.complete is False
    assert panel.current_field.key == "note"

    done = panel.submit_text("保留旧接口兼容")
    assert done.complete is True
    assert done.answers == {"scope": "both", "note": "保留旧接口兼容"}


def test_ask_panel_rejects_custom_text_when_disabled():
    panel = AskPanel()
    panel.update_request(AskRequest(
        id="ask-test-2",
        fields=(AskField(
            key="choice",
            question="请选择",
            options=(AskOption("是", "yes"),),
            allow_custom=False,
            required=True,
        ),),
    ))

    result = panel.submit_text("不确定")
    assert result.complete is False
    assert result.error
    assert panel.submit_text("1").complete is True


def test_reducer_collapses_ask_tool_call_while_waiting_for_answer():
    state = TuiState(active_turn_id="turn-1", phase="running")
    call = ToolCall(
        id="call-ask",
        name="ask_user",
        arguments='{"fields": []}',
    )
    state = reduce_agent_event(
        state,
        AgentEvent(kind="tool_call", tool_call=call),
        "turn-1",
    )
    assert state.transcript[-1].collapsed is False

    state = reduce_agent_event(
        state,
        AgentEvent(kind="ask_user", ask=make_request()),
        "turn-1",
    )
    tool_call = next(item for item in state.transcript if item.kind == "tool_call")
    assert tool_call.collapsed is True
    assert state.phase == "awaiting_ask"


@pytest.mark.asyncio
async def test_tui_ask_panel_is_inline_and_keeps_one_screen(tmp_path, monkeypatch):
    # Controller startup calibration writes the user's global adaptive store;
    # keep this focused UI test fully local and side-effect free.
    monkeypatch.setattr("xg.adaptive.calibrate.recalibrate", lambda: None)
    monkeypatch.setattr("xg.adaptive.learned_rules.re_learn", lambda: None)
    project = tmp_path / "project"
    project.mkdir()
    manager = ConfigManager(
        user_dir=tmp_path / "user",
        project_dir=project,
        env={},
        load_env=False,
    )
    settings = Settings(
        provider="test",
        model="test-model",
        api_base="https://example.test",
        context_window=128_000,
    )
    agent = ReActAgent(EmptyClient(), build_registry(base_dir=project), settings)
    app = XgTuiApp(agent, settings, manager)

    async with app.run_test(size=(120, 30)) as pilot:
        ask = make_request()
        app.controller._set_state(replace(
            app.controller.state,
            phase="awaiting_ask",
            pending_ask=ask,
        ))
        app._state = app.controller.snapshot()
        app._render_state(app._state)

        panel = app.query_one("#ask-panel", AskPanel)
        composer = app.query_one("#composer")
        assert panel.display is True
        assert composer.ask_mode is True
        assert len(app.screen_stack) == 1
        assert composer.region.bottom >= app.size.height - 1

        await pilot.press("down")
        assert panel._view.selected_option == 1
        await pilot.press("enter")
        await pilot.pause()
        assert panel.current_field.key == "note"
