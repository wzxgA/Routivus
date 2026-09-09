"""MCP trust boundary, HITL defaults and audit redaction."""

from __future__ import annotations

import json

from routivus.config.mcp import McpConfigManager
from routivus.mcp.manager import McpManager
from routivus.safety.audit import AuditLogger
from routivus.safety.hitl import HITLPolicy
from routivus.tool.registry import ToolRegistry

from tests.test_mcp_manager import FakeTransport


async def test_mcp_tools_default_to_confirm_and_audit_redacts_args_and_logs(tmp_path):
    config_path = tmp_path / ".xg" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "servers": {"demo": {"transport": "stdio", "command": "fake"}}
    }), encoding="utf-8")
    audit_path = tmp_path / ".xg" / "audit.log"
    audit = AuditLogger(audit_path)
    policy = HITLPolicy()
    registry = ToolRegistry(audit=audit)
    transports = {}

    def factory(config):
        transport = FakeTransport(config)
        transport.log("Authorization: Bearer top-secret")
        transports[config.name] = transport
        return transport

    manager = McpManager(
        registry,
        McpConfigManager(user_dir=tmp_path / "user", project_root=tmp_path, env={}),
        approval_policy=policy,
        audit=audit,
        transport_factory=factory,
    )
    await manager.start_all()
    assert policy.requires_approval("mcp__demo__echo_value")
    result = await registry.aexecute(
        "mcp__demo__echo_value", {"value": "ok", "token": "top-secret"}
    )
    assert result.ok
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    tool_record = next(record for record in records if record["action"] == "tool_call")
    assert tool_record["args"]["token"] == "***"
    assert "top-secret" not in manager.logs("demo")
    await manager.close()

