"""测试 xg/cli/path_heal.py 首次运行自愈 PATH。"""

from __future__ import annotations

import os
import sysconfig

import pytest

from xg.cli import path_heal
from xg.cli.path_heal import ensure_on_path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(path_heal.ENV_FLAG, raising=False)
    monkeypatch.setattr(path_heal, "_persist_contains", lambda e: False)
    monkeypatch.setattr(path_heal, "_write", lambda e: None)
    monkeypatch.setattr(path_heal, "_notify_win", lambda: None)
    # 写入/幂等类用例默认认为命令文件已存在，聚焦 PATH 写入逻辑。
    monkeypatch.setattr(path_heal, "_has_dispatch", lambda _d: True)


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv(path_heal.ENV_FLAG, "0")
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "/tmp/scripts")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    assert ensure_on_path() is False


def test_no_ops_when_no_scripts(monkeypatch):
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "")
    assert ensure_on_path() is False


def test_no_ops_when_no_dispatch_file(monkeypatch):
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "/x/scripts")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(path_heal, "_has_dispatch", lambda _d: False)
    assert ensure_on_path() is False


def test_no_ops_when_already_persisted(monkeypatch):
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "/x/scripts")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(path_heal, "_persist_contains", lambda e: True)
    assert ensure_on_path() is False


def test_writes_and_updates_current_path(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "C:\\scripts\\xg")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(path_heal, "_write", lambda e: written.append(e))
    monkeypatch.setattr(os, "name", "nt")

    assert ensure_on_path() is True
    assert written == ["C:\\scripts\\xg"]
    # 当前进程 PATH 已立即包含
    assert path_heal._seg_contains(os.environ.get("PATH", ""), "C:\\scripts\\xg")


def test_write_failure_returns_false(monkeypatch):
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "/x/scripts")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(path_heal, "_write", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ensure_on_path() is False


def test_seg_contains():
    sep = os.pathsep
    joined = sep.join(["", "/a", "/b", "/c", ""])
    assert path_heal._seg_contains(joined, "/b") is True
    assert path_heal._seg_contains(sep.join(["/a", "/b", "/c"]), "/b/") is False
    assert path_heal._seg_contains("", "/b") is False


def test_rc_add_appends_to_profile_when_no_rc(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(path_heal, "_home", lambda: str(home))
    monkeypatch.setattr(path_heal, "_rc_files", lambda: [])
    path_heal._rc_add("/x/scripts")
    content = (home / ".profile").read_text(encoding="utf-8")
    assert 'export PATH="/x/scripts:$PATH"' in content


def test_rc_add_appends_to_existing_rc(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".bashrc"
    rc.write_text("# existing\n", encoding="utf-8")
    monkeypatch.setattr(path_heal, "_home", lambda: str(home))
    monkeypatch.setattr(path_heal, "_rc_files", lambda: [str(rc)])
    path_heal._rc_add("/x/scripts")
    assert 'export PATH="/x/scripts:$PATH"' in rc.read_text(encoding="utf-8")


def test_rc_contains_detects_existing_entry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".bashrc"
    rc.write_text('export PATH="/x/scripts:$PATH"\n', encoding="utf-8")
    monkeypatch.setattr(path_heal, "_rc_files", lambda: [str(rc)])
    assert path_heal._rc_contains("/x/scripts") is True
    assert path_heal._rc_contains("/y/scripts") is False


def test_dispatch_path_detects_windows_exe(tmp_path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    exe = scripts / "xg-cli.exe"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(path_heal, "_has_dispatch", lambda _d: bool(path_heal._dispatch_path(_d)))
    assert path_heal._dispatch_path(str(scripts)) == str(exe)
    assert path_heal._has_dispatch(str(scripts)) is True


def test_dispatch_path_empty_when_missing(tmp_path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    monkeypatch.setattr(path_heal, "_has_dispatch", lambda _d: bool(path_heal._dispatch_path(_d)))
    assert path_heal._dispatch_path(str(scripts)) == ""
    assert path_heal._has_dispatch(str(scripts)) is False


def test_scripts_dir_prefers_which(monkeypatch):
    # shutil.which 能解析 → 直接取父目录，不再回退 sysconfig
    monkeypatch.setattr(path_heal, "_which_dispatch", lambda: "/real/bin/xg-cli")
    monkeypatch.setattr(path_heal, "_record_dispatch", lambda: "/record/bin/xg-cli")
    assert path_heal._scripts_dir() == "/real/bin"


def test_scripts_dir_falls_back_to_record(monkeypatch):
    # which 为空、RECORD 命中 → 用 RECORD 的父目录（微软商店版关键路径）
    monkeypatch.setattr(path_heal, "_which_dispatch", lambda: "")
    monkeypatch.setattr(path_heal, "_record_dispatch", lambda: "/user/Scripts/xg-cli")
    assert path_heal._scripts_dir() == "/user/Scripts"


def test_scripts_dir_falls_back_to_sysconfig(monkeypatch):
    # which / RECORD 都取不到 → 兜底 sysconfig
    monkeypatch.setattr(path_heal, "_which_dispatch", lambda: "")
    monkeypatch.setattr(path_heal, "_record_dispatch", lambda: "")
    monkeypatch.setattr(sysconfig, "get_path", lambda name: "/sys/src/scripts")
    assert path_heal._scripts_dir() == "/sys/src/scripts"


def test_scripts_dir_empty_on_sysconfig_error(monkeypatch):
    monkeypatch.setattr(path_heal, "_which_dispatch", lambda: "")
    monkeypatch.setattr(path_heal, "_record_dispatch", lambda: "")

    def _boom(_name):
        raise ValueError("no scripts")

    monkeypatch.setattr(sysconfig, "get_path", _boom)
    assert path_heal._scripts_dir() == ""


def test_record_dispatch_finds_check_family(monkeypatch, tmp_path):
    # 模拟 pip 写入的 RECORD 含 xg-cli 实际路径（形如 .../Scripts/xg-cli.exe）
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "xg-cli.exe").write_text("x", encoding="utf-8")

    class _FakeDistFile:
        name = f"../../../Scripts/xg-cli.exe"

    class _FakeDist:
        files = [_FakeDistFile()]

        def locate_file(self, name):
            return scripts / "xg-cli.exe"

    import importlib.metadata as md

    monkeypatch.setattr(md, "distribution", lambda _name: _FakeDist())
    assert path_heal._record_dispatch() == str(scripts / "xg-cli.exe")


def test_record_dispatch_empty_wing_dists(monkeypatch):
    import importlib.metadata as md

    monkeypatch.setattr(md, "distribution", lambda _name: (_ for _ in ()).throw(md.PackageNotFoundError("no")))
    assert path_heal._record_dispatch() == ""