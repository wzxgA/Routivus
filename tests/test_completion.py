"""Pure unit tests for the P0/P1 completion module (routivus.cli.completion)."""

from __future__ import annotations

from routivus.cli.completion import (
    CompletionCandidate,
    apply_completion,
    complete_command_token,
    completion_candidates,
    parse_completion_line,
    replace_span,
    sort_candidates,
    tokenize,
)


# --- tokenize ---


def test_tokenize_splits_on_whitespace_with_offsets():
    tokens = tokenize("/model deepseek/deepseek-chat")
    assert [(t.text, t.start, t.end) for t in tokens] == [
        ("/model", 0, 6),
        ("deepseek/deepseek-chat", 7, 29),
    ]


def test_tokenize_keeps_quoted_segments_together():
    tokens = tokenize('/web search "Python 3.13 新特性"')
    assert [(t.text, t.start, t.end) for t in tokens] == [
        ("/web", 0, 4),
        ("search", 5, 11),
        ('"Python 3.13 新特性"', 12, 29),
    ]


def test_tokenize_tolerates_unterminated_quote():
    tokens = tokenize('/web search "未闭合')
    assert [(t.text, t.start, t.end) for t in tokens] == [
        ("/web", 0, 4),
        ("search", 5, 11),
        ('"未闭合', 12, 16),
    ]


def test_tokenize_empty_and_leading_whitespace():
    assert tokenize("") == []
    assert [(t.text, t.start) for t in tokenize("   /plan")] == [("/plan", 3)]


# --- parse_completion_line ---


def test_parse_detects_command_context():
    ctx = parse_completion_line("/model deepseek")
    assert ctx.is_command is True
    assert ctx.command == "/model"
    assert ctx.command_token_start == 0
    assert ctx.command_token_end == 6


def test_parse_command_token_offsets_account_for_leading_whitespace():
    ctx = parse_completion_line("  /team resume")
    assert ctx.is_command is True
    assert ctx.command == "/team"
    assert ctx.command_token_start == 2
    assert ctx.command_token_end == 7


def test_parse_non_command_is_false():
    assert parse_completion_line("实现登录模块").is_command is False
    assert parse_completion_line("").is_command is False


def test_parse_current_token_at_cursor():
    ctx = parse_completion_line("/team res", cursor_position=6)
    assert ctx.current_token == "res"
    assert ctx.current_token_start == 6
    assert ctx.current_token_end == 9


def test_parse_cursor_clamped_to_input_length():
    ctx = parse_completion_line("/team resume", cursor_position=999)
    assert ctx.cursor_position == len("/team resume")


def test_parse_cursor_after_trailing_space_has_no_current_token():
    ctx = parse_completion_line("/team resume ", cursor_position=len("/team resume "))
    assert ctx.current_token == ""
    # Insertion point becomes the cursor position itself.
    assert ctx.current_token_start == len("/team resume ")
    assert ctx.current_token_end == len("/team resume ")


# --- complete_command_token ---


def test_complete_replaces_leading_token_preserving_trailing_text():
    value, cursor = complete_command_token("/team res 实现模块", "/team resume")
    assert value == "/team resume res 实现模块"
    assert cursor == len(value)


def test_complete_preserves_leading_whitespace():
    value, cursor = complete_command_token("  /m 之后", "/model")
    assert value == "  /model 之后"
    assert cursor == len(value)


def test_complete_non_command_is_unchanged():
    value, cursor = complete_command_token("普通文本", "/model")
    assert value == "普通文本"
    assert cursor == len(value)


# --- replace_span ---


def test_replace_span_mid_token_preserves_suffix():
    new, cur = replace_span("xg/au extra", 0, 5, "xg/auth/")
    assert new == "xg/auth/ extra"
    assert cur == len("xg/auth/")


# --- sort_candidates / CompletionCandidate ---


def test_candidate_sort_floats_exact_match_first():
    candidates = [
        CompletionCandidate("/cancel", "/cancel", detail="取消任务"),
        CompletionCandidate("/config", "/config", detail="配置"),
        CompletionCandidate("/clear", "/clear", detail="清屏"),
    ]
    ordered = sort_candidates(candidates, exact_match="/cancel")
    assert [c.insert_text for c in ordered] == ["/cancel", "/clear", "/config"]


def test_candidate_sort_respects_sort_key_then_label():
    candidates = [
        CompletionCandidate("b", "b", detail="", sort_key=(1, "z")),
        CompletionCandidate("a", "a", detail="", sort_key=(0, "")),
    ]
    ordered = sort_candidates(candidates)
    assert [c.insert_text for c in ordered] == ["a", "b"]


# ---------------------------------------------------------------------------
# P1: layered command / subcommand / option completion
# ---------------------------------------------------------------------------


def test_p1_top_level_command_candidates_preserve_compat():
    candidates = completion_candidates("/m")
    assert [c.insert_text for c in candidates] == ["/model", "/mcp", "/memory"]
    assert all(c.kind == "command" for c in candidates)


def test_p1_no_candidates_for_plain_text():
    assert completion_candidates("实现登录模块") == []


def test_p1_subcommand_completion_for_skill():
    candidates = completion_candidates("/skill di")
    assert [c.insert_text for c in candidates] == ["disable"]
    assert candidates[0].kind == "subcommand"


def test_p1_subcommand_completion_for_mcp():
    candidates = completion_candidates("/mcp sta")
    assert [c.insert_text for c in candidates] == ["status"]
    assert candidates[0].kind == "subcommand"


def test_p1_subcommand_completion_offers_all_when_prefix_unmatched():
    # /team 只剩一个子命令（run）：前缀对不上时仍列出全部子命令
    candidates = completion_candidates("/team xyz")
    assert [c.insert_text for c in candidates] == ["run"]


def test_p1_blank_argument_slot_offers_all_subcommands():
    # An empty second-token slot surfaces every subcommand of the command.
    candidates = completion_candidates("/team ", cursor_position=len("/team "))
    assert [c.insert_text for c in candidates] == ["run"]


def test_p1_apply_completion_replaces_current_token():
    value, cursor = apply_completion(
        "/team res", len("/team res"), CompletionCandidate("resume", "resume", kind="subcommand")
    )
    assert value == "/team resume"
    assert cursor == len(value)


def test_p1_apply_completion_option_adds_trailing_space():
    value, cursor = apply_completion(
        "/train --y",
        None,
        CompletionCandidate("--yes", "--yes ", kind="option"),
    )
    assert value == "/train --yes "
    assert cursor == len(value)


def test_p1_apply_completion_preserves_non_command_text():
    value, cursor = apply_completion(
        "普通文本", None, CompletionCandidate("/a", "/a", kind="command")
    )
    assert value == "普通文本"
    assert cursor == len(value)


# ---------------------------------------------------------------------------
# P2: dynamic local value completion
# ---------------------------------------------------------------------------


def _registry():
    from routivus.cli.completion import CompletionProviderRegistry, CompletionCandidate

    reg = CompletionProviderRegistry()
    reg.register(
        "provider",
        lambda ctx: [CompletionCandidate(n, n, kind="value") for n in
                     ("openai", "deepseek", "glm")],
    )
    reg.register(
        "model",
        lambda ctx: [CompletionCandidate(m, m, kind="value") for m in
                     ("deepseek-chat", "gpt-4o-mini", "glm-4-flash")],
    )
    reg.register(
        "mcp", lambda ctx: [CompletionCandidate("local", "local", kind="value")]
    )
    reg.register(
        "skill", lambda ctx: [CompletionCandidate("code-review", "code-review", kind="value")]
    )
    reg.register(
        "memory_id", lambda ctx: [CompletionCandidate("1", "1", kind="value")]
    )
    return reg


def test_p2_dynamic_provider_candidates_for_model():
    from routivus.cli.completion import dynamic_candidates

    got = dynamic_candidates("/model dee", None, _registry())
    assert [c.insert_text for c in got] == ["openai", "deepseek", "glm"]


def test_p2_dynamic_mcp_server_candidates():
    from routivus.cli.completion import dynamic_candidates

    got = dynamic_candidates("/mcp restart ", len("/mcp restart "), _registry())
    assert [c.insert_text for c in got] == ["local"]


def test_p2_dynamic_skill_candidates():
    from routivus.cli.completion import dynamic_candidates

    got = dynamic_candidates("/skill disable co", None, _registry())
    assert [c.insert_text for c in got] == ["code-review"]


def test_p2_dynamic_memory_id_candidates():
    from routivus.cli.completion import dynamic_candidates

    got = dynamic_candidates("/memory delete 1", None, _registry())
    assert [c.insert_text for c in got] == ["1"]


def test_p2_dynamic_result_is_capped():
    from routivus.cli.completion import dynamic_candidates

    reg = _registry()
    got = dynamic_candidates("/model ", len("/model "), reg, limit=2)
    assert len(got) <= 2


def test_p2_dynamic_returns_empty_for_non_command():
    from routivus.cli.completion import dynamic_candidates

    assert dynamic_candidates("普通文本", None, _registry()) == []


def test_p2_dynamic_missing_provider_never_raises():
    from routivus.cli.completion import CompletionProviderRegistry, dynamic_candidates

    reg = CompletionProviderRegistry()  # empty registry
    assert dynamic_candidates("/mcp restart lo", None, reg) == []


def test_p2_dynamic_failing_provider_degrades_to_empty():
    from routivus.cli.completion import CompletionProviderRegistry, dynamic_candidates

    def boom(_ctx):
        raise RuntimeError("provider failure")

    reg = CompletionProviderRegistry()
    reg.register("mcp", boom)
    assert dynamic_candidates("/mcp restart lo", None, reg) == []
