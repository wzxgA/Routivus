"""MCP tool schema normalization tests."""

from routivus.mcp.schema import exposed_tool_name, sanitize_schema


def test_local_ref_is_expanded_and_required_is_filtered():
    schema, warnings = sanitize_schema({
        "type": "object",
        "$defs": {"Repo": {"type": "string", "description": "repository"}},
        "properties": {"repo": {"$ref": "#/$defs/Repo"}},
        "required": ["repo", "missing"],
    })
    assert schema["properties"]["repo"]["type"] == "string"
    assert schema["required"] == ["repo"]
    assert not warnings


def test_anyof_nullable_collapses_and_cycle_is_safe():
    schema, warnings = sanitize_schema({
        "type": "object",
        "$defs": {"Loop": {"$ref": "#/$defs/Loop"}},
        "properties": {
            "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "loop": {"$ref": "#/$defs/Loop"},
        },
    })
    assert schema["properties"]["value"]["type"] == "string"
    assert schema["properties"]["loop"]["type"] == "string"
    assert any("$ref" in warning for warning in warnings)


def test_tool_name_is_namespaced_and_provider_safe():
    assert exposed_tool_name("git-hub", "issues/list.v2") == "mcp__git-hub__issues_list_v2"

