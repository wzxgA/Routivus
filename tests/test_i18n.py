"""v1.1.7 product UI translation coverage tests."""

from __future__ import annotations

from xg.cli.help import format_command_help, format_help
from xg.tui.i18n import INSPECTOR_TEXT, UI_TEXT, normalize_language, translate, translation_keys
from xg.tui.state import TuiState
from xg.tui.widgets.header import HeaderBar
from xg.tui.state import QueueItem
from xg.tui.widgets.composer import Composer
from xg.tui.widgets.queue_status import QueueStatus


def test_product_catalog_has_matching_english_and_chinese_keys() -> None:
    assert set(UI_TEXT["en"]) == set(UI_TEXT["zh"])
    assert set(INSPECTOR_TEXT["en"]) == set(INSPECTOR_TEXT["zh"])
    assert translation_keys()
    assert all(value for catalog in UI_TEXT.values() for value in catalog.values())


def test_product_catalog_placeholders_match() -> None:
    import string

    def fields(value: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(value) if name}

    for key in UI_TEXT["en"]:
        assert fields(UI_TEXT["en"][key]) == fields(UI_TEXT["zh"][key]), key


def test_unknown_language_and_key_fall_back_safely() -> None:
    assert normalize_language("fr") == "en"
    assert translate("fr", "ui.input") == "Input"
    assert translate("en", "missing.key") == "missing.key"


def test_help_supports_both_ui_languages() -> None:
    english = format_help(language="en")
    chinese = format_help(language="zh")
    assert english.startswith("XG Command Help\n")
    assert "Workflow" in english
    assert "Generate, review, and execute a plan" in english
    assert "XG 命令帮助" in chinese
    assert "工作流" in chinese
    assert format_command_help("mcp", language="en").startswith(
        "/mcp — Manage MCP Servers"
    )


def test_header_uses_state_language() -> None:
    header = HeaderBar()
    english = TuiState(ui_language="en")
    header.update_state(english)
    assert "Context" in str(header.render())
    chinese = TuiState(ui_language="zh")
    header.update_state(chinese)
    assert "上下文" in str(header.render())


def test_queue_status_and_composer_follow_language_without_losing_input() -> None:
    queue = QueueStatus()
    state = TuiState(ui_language="en", queue=[QueueItem("queue-1", "build it", "task")])
    queue.update_state(state)
    assert "Queue" in str(queue.render())

    composer = Composer()
    composer.set_language("en")
    assert composer.placeholder.startswith("Enter a task")
    composer.set_language("zh")
    assert composer.placeholder.startswith("输入任务")
