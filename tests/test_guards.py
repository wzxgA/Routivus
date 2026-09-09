"""PathGuard / CommandGuard 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from routivus.safety.guards import command_guard, guard_tool_call, path_guard


class TestCommandGuard:
    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /",
            "sudo rm -rf /",
            "rm -rf /*",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            "format c:",
            "format C:\\",
            "del /f /q C:\\windows\\system32",
            "rd /s /q C:\\",
            "shutdown /s",
            "reboot",
            "halt",
            "poweroff",
            ":(){ :|:& };:",
        ],
    )
    def test_blacklist_hits(self, cmd):
        result = command_guard(cmd)
        assert not result.ok
        assert result.reason == "command_blacklist"

    @pytest.mark.parametrize(
        "cmd",
        [
            "dir",
            "echo hello",
            "rm -rf ./build",
            "rm -rf build/",
            "git status",
            "uv run pytest",
            "ls -la",
            "del /f build\\tmp.txt",
        ],
    )
    def test_safe_commands_pass(self, cmd):
        assert command_guard(cmd).ok

    def test_empty_command_passes(self):
        assert command_guard("").ok
        assert command_guard("   ").ok


class TestPathGuard:
    def test_in_root_passes(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        result = path_guard(tmp_path, "read_file", {"path": "src/main.py"})
        assert result.ok

    def test_relative_escape_blocked(self, tmp_path: Path):
        result = path_guard(tmp_path, "read_file", {"path": "../secrets.txt"})
        assert not result.ok
        assert result.reason == "path_outside_root"

    def test_absolute_escape_blocked(self, tmp_path: Path):
        result = path_guard(tmp_path, "read_file", {"path": str(tmp_path.parent / "x.txt")})
        assert not result.ok

    def test_symlink_escape_blocked(self, tmp_path: Path):
        outside = tmp_path.parent / f"outside_{tmp_path.name}.txt"
        outside.write_text("secret", encoding="utf-8")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("当前环境无权限创建符号链接")
        try:
            result = path_guard(tmp_path, "read_file", {"path": "link.txt"})
            assert not result.ok
            assert result.reason == "path_outside_root"
        finally:
            outside.unlink(missing_ok=True)

    def test_write_file_parent_check(self, tmp_path: Path):
        result = path_guard(tmp_path, "write_file", {"path": "out/x.txt", "content": "x"})
        assert result.ok

    def test_write_file_escape_blocked(self, tmp_path: Path):
        result = path_guard(tmp_path, "write_file", {"path": "../evil.txt", "content": "x"})
        assert not result.ok

    def test_non_path_tool_always_ok(self, tmp_path: Path):
        assert path_guard(tmp_path, "no_such_tool", {}).ok


class TestGuardToolCall:
    def test_command_tool_routes_to_command_guard(self, tmp_path: Path):
        assert not guard_tool_call(tmp_path, "execute_command", {"command": "rm -rf /"}).ok
        assert guard_tool_call(tmp_path, "execute_command", {"command": "dir"}).ok

    def test_path_tool_routes_to_path_guard(self, tmp_path: Path):
        assert guard_tool_call(tmp_path, "read_file", {"path": "../x"}).reason == "path_outside_root"
        assert guard_tool_call(tmp_path, "read_file", {"path": "a.py"}).ok
