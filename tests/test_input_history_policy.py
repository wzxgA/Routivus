from __future__ import annotations

import pytest

from routivus.input_history.policy import is_sensitive, normalize_text, should_record


@pytest.mark.parametrize("text", [
    "api_key=secret", "password: secret", "Authorization: Bearer abc",
    "Bearer abc", "sk-abcdefghijklmnop", "/save 不要持久化",
    "/config set providers.openai.api_key secret",
])
def test_sensitive_inputs_are_detected(text):
    assert is_sensitive(text) is True


@pytest.mark.parametrize("text", ["", "   ", "/cancel", "/c", "/exit", "/quit", "/history status"])
def test_empty_and_control_inputs_are_not_history_records(text):
    assert should_record(text) is False


def test_normalize_text_matches_submitted_cli_value():
    assert normalize_text("  查看 README  ") == "查看 README"
