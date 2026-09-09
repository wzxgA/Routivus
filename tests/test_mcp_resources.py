"""MCP resource reference parsing, binary fallback and URI redaction."""

from routivus.mcp.resources import decode_resource_contents, find_resource_references, redact_uri


def test_reference_parser_keeps_server_and_uri_and_trims_punctuation():
    refs = find_resource_references("看 @docs:file:///guide.md，再看 @git:repo://issues/1。")
    assert [(item.server, item.uri) for item in refs] == [
        ("docs", "file:///guide.md"),
        ("git", "repo://issues/1"),
    ]


def test_binary_resource_is_not_inlined_and_text_is_limited():
    text, limited = decode_resource_contents(
        {"contents": [{"blob": "aGVsbG8=", "mimeType": "application/octet-stream"}, {"text": "x" * 20}]},
        10,
    )
    assert limited
    assert len(text) < 100
    assert "截断" in text


def test_uri_redaction_removes_userinfo_and_query():
    assert redact_uri("https://user:secret@example.test/path?token=secret") == "https://example.test/path"

