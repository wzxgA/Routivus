"""AuditLogger 单元测试：JSONL 结构 + 脱敏。"""

from __future__ import annotations

import json
from pathlib import Path

from routivus.safety.audit import AuditLogger, redact


class TestRedact:
    def test_sensitive_keys_masked(self):
        result = redact({"api_key": "sk-secret", "command": "dir", "token": "abc"})
        assert result["api_key"] == "***"
        assert result["token"] == "***"
        assert result["command"] == "dir"

    def test_nested_dict_and_list(self):
        result = redact({"args": {"password": "p", "items": [{"authorization": "x"}]}})
        assert result["args"]["password"] == "***"
        assert result["args"]["items"][0]["authorization"] == "***"

    def test_bearer_token_in_text_masked(self):
        text = "Authorization: Bearer sk-abcdef123"
        assert "sk-abcdef123" not in redact(text)
        assert "Bearer ***" in redact(text)


class TestAuditLogger:
    def test_tool_call_record(self, tmp_path: Path):
        log_path = tmp_path / ".xg" / "audit.log"
        logger = AuditLogger(log_path, session_id="s1")
        logger.tool_call(tool="read_file", args={"path": "a.py"}, ok=True, duration_ms=12)

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "tool_call"
        assert entry["session"] == "s1"
        assert entry["tool"] == "read_file"
        assert entry["ok"] is True
        assert entry["duration_ms"] == 12
        assert "time" in entry

    def test_approval_and_blocked(self, tmp_path: Path):
        log_path = tmp_path / "audit.log"
        logger = AuditLogger(log_path, session_id="s2")
        logger.approval(tool="execute_command", args={"command": "dir"}, decision="deny", reason="user_rejected")
        logger.blocked(reason="command_blacklist", tool="execute_command", args={"command": "rm -rf /"})

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(lines[0])["action"] == "approval"
        assert json.loads(lines[1])["action"] == "blocked"

    def test_args_redacted_on_write(self, tmp_path: Path):
        log_path = tmp_path / "audit.log"
        logger = AuditLogger(log_path, session_id="s3")
        logger.tool_call(tool="write_file", args={"path": "c.txt", "api_key": "sk-leak"}, ok=True, duration_ms=1)

        entry = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert entry["args"]["api_key"] == "***"
        assert "sk-leak" not in log_path.read_text(encoding="utf-8")

    def test_bearer_redacted_in_output_fields(self, tmp_path: Path):
        log_path = tmp_path / "audit.log"
        logger = AuditLogger(log_path)
        logger.record("raw", text="Authorization: Bearer sk-token123")
        content = log_path.read_text(encoding="utf-8")
        assert "sk-token123" not in content

    def test_logger_ignores_os_errors(self, tmp_path: Path):
        logger = AuditLogger(tmp_path / "no_such_dir" / "x" / "audit.log")
        logger.tool_call(tool="read_file", args={}, ok=True, duration_ms=1)  # 不应抛异常
