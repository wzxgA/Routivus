"""内置工具单元测试：正常路径 + 异常路径。"""

from __future__ import annotations

import json
from pathlib import Path


class TestReadFile:
    def test_read_with_line_numbers(self, registry, tmp_project):
        result = registry.execute("read_file", {"path": "src/main.py"})
        assert result.ok
        assert "1→def main():" in result.output
        assert "2→    print('hello')" in result.output

    def test_offset_limit(self, registry):
        result = registry.execute("read_file", {"path": "src/main.py", "offset": 2, "limit": 1})
        assert result.ok
        assert "2→    print('hello')" in result.output
        assert "1→def main():" not in result.output

    def test_missing_file(self, registry):
        result = registry.execute("read_file", {"path": "no_such.py"})
        assert not result.ok
        assert "不存在" in result.error


class TestWriteFile:
    def test_write_and_read_back(self, registry, tmp_project):
        result = registry.execute("write_file", {"path": "src/new.py", "content": "x = 1\n"})
        assert result.ok
        assert (tmp_project / "src" / "new.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_missing_parent_dir(self, registry):
        result = registry.execute("write_file", {"path": "a/b/c.txt", "content": "x"})
        assert not result.ok
        assert "父目录不存在" in result.error


class TestListDir:
    def test_list_entries(self, registry):
        result = registry.execute("list_dir", {})
        assert result.ok
        assert "src" in result.output
        assert "README.md" in result.output

    def test_ignores_special_dirs(self, registry, tmp_project):
        (tmp_project / ".git").mkdir()
        (tmp_project / ".git" / "config").write_text("x", encoding="utf-8")
        result = registry.execute("list_dir", {})
        assert result.ok
        assert ".git" not in result.output

    def test_missing_dir(self, registry):
        result = registry.execute("list_dir", {"path": "no_such_dir"})
        assert not result.ok


class TestGlobFiles:
    def test_glob_py(self, registry):
        result = registry.execute("glob_files", {"pattern": "**/*.py"})
        assert result.ok
        assert "src/main.py" in result.output
        assert "src/util.py" in result.output
        assert "README.md" not in result.output

    def test_no_match(self, registry):
        result = registry.execute("glob_files", {"pattern": "**/*.rs"})
        assert result.ok
        assert "无匹配" in result.output


class TestGrepCode:
    def test_grep_pattern(self, registry):
        result = registry.execute("grep_code", {"pattern": r"def \w+"})
        assert result.ok
        assert "src/main.py:1: def main():" in result.output
        assert "src/util.py:1: def add(a, b):" in result.output

    def test_grep_with_glob_filter(self, registry):
        result = registry.execute("grep_code", {"pattern": "hello", "glob": "*.md"})
        assert result.ok
        assert "无匹配" in result.output

    def test_invalid_regex(self, registry):
        result = registry.execute("grep_code", {"pattern": "("})
        assert not result.ok
        assert "无效正则" in result.error


class TestExecuteCommand:
    def test_echo(self, registry):
        result = registry.execute("execute_command", {"command": "echo hello_xg"})
        assert result.ok
        assert "hello_xg" in result.output

    def test_failing_command(self, registry):
        result = registry.execute("execute_command", {"command": "exit 3"})
        assert not result.ok
        assert "退出码 3" in result.error

    def test_writes_audit_log(self, tmp_project):
        from routivus.safety.audit import AuditLogger
        from routivus.tool.builtin import build_registry

        audit = AuditLogger(tmp_project / ".xg" / "audit.log")
        reg = build_registry(base_dir=tmp_project, audit=audit)
        reg.execute("execute_command", {"command": "echo audit_me"})
        log = tmp_project / ".xg" / "audit.log"
        assert log.is_file()
        record = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert record["action"] == "tool_call"
        assert record["tool"] == "execute_command"

    def test_timeout(self, registry):
        result = registry.execute(
            "execute_command", {"command": "ping -n 10 127.0.0.1", "timeout": 1}
        )
        assert not result.ok
        assert "超时" in result.error


class TestRegistry:
    def test_unknown_tool(self, registry):
        result = registry.execute("no_such_tool", {})
        assert not result.ok
        assert "未知工具" in result.error

    def test_output_truncation(self, tmp_project):
        from routivus.tool.builtin import build_registry

        reg = build_registry(base_dir=tmp_project, max_output_chars=100)
        (tmp_project / "big.txt").write_text("a" * 500, encoding="utf-8")
        result = reg.execute("read_file", {"path": "big.txt"})
        assert result.ok
        assert len(result.output) < 200
        assert "截断" in result.output

    def test_execute_calls_preserves_order(self, registry, tmp_project):
        from routivus.llm.types import ToolCall

        calls = [
            ToolCall(id="1", name="write_file", arguments=json.dumps({"path": "o.txt", "content": "one"})),
            ToolCall(id="2", name="write_file", arguments=json.dumps({"path": "t.txt", "content": "two"})),
        ]
        results = registry.execute_calls(calls)
        assert [r.tool_call_id for r in results] == ["1", "2"]
        assert all(r.ok for r in results)
        assert (tmp_project / "o.txt").exists()
        assert (tmp_project / "t.txt").exists()
