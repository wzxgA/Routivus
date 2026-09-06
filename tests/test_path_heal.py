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


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv(path_heal.ENV_FLAG, "0")
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "/tmp/scripts")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    assert ensure_on_path() is False


def test_no_ops_when_no_scripts(monkeypatch):
    monkeypatch.setattr(path_heal, "_scripts_dir", lambda: "")
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