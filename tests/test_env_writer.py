"""测试 :mod:`xg.config.env_writer`。"""

from __future__ import annotations

from pathlib import Path

from routivus.config.env_writer import (
    decide_env_path,
    env_value,
    find_env_file,
    set_env_key,
    upsert_env_key,
    write_env_atomic,
)


def test_upsert_appends_when_missing():
    lines = ["A=1", "# comment"]
    out = upsert_env_key(lines, "B", "2")
    assert out == ["A=1", "# comment", "B=2"]


def test_upsert_replaces_existing_preserving_position():
    lines = ["A=1", "XG_FOO=old", "C=3"]
    out = upsert_env_key(lines, "XG_FOO", "new")
    assert out == ["A=1", "XG_FOO=new", "C=3"]


def test_upsert_dedupes_repeated():
    lines = ["XG_FOO=a", "keep", "XG_FOO=b"]
    out = upsert_env_key(lines, "XG_FOO", "c")
    assert out == ["XG_FOO=c", "keep"]


def test_env_value_handles_quotes_and_spaces():
    lines = ['XG_FOO = "sk ab"', "XG_BAR=plain"]
    assert env_value(lines, "XG_FOO") == "sk ab"
    assert env_value(lines, "XG_BAR") == "plain"
    assert env_value(lines, "MISSING") is None


def test_find_env_file_walks_up(tmp_path: Path):
    leaf = tmp_path / "a" / "b"
    leaf.mkdir(parents=True)
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
    assert find_env_file(leaf) == tmp_path / ".env"


def test_decide_env_path_prefers_existing(tmp_path: Path):
    # 场景 1：ancestor 存在 .env -> 命中
    root_a = tmp_path / "root_a"
    leaf = root_a / "b"
    leaf.mkdir(parents=True)
    (root_a / ".env").write_text("K=v\n", encoding="utf-8")
    assert decide_env_path(leaf, tmp_path / "fallback") == root_a / ".env"
    # 场景 2：ancestor 无 .env -> 回退到 fallback 目录
    isolated = tmp_path / "root_b" / "iso"
    isolated.mkdir(parents=True)
    assert decide_env_path(isolated, tmp_path / "fallback") == tmp_path / "fallback" / ".env"


def test_set_env_key_append_then_overwrite(tmp_path: Path):
    target = tmp_path / ".env"
    changed, prev = set_env_key(target, "XG_FOO", "v1")
    assert changed is True and prev is None
    assert env_value(target.read_text(encoding="utf-8").splitlines(), "XG_FOO") == "v1"

    # 已存在且 overwrite=False -> 不改、返回旧值
    changed, prev = set_env_key(target, "XG_FOO", "v2")
    assert changed is False and prev == "v1"
    assert env_value(target.read_text(encoding="utf-8").splitlines(), "XG_FOO") == "v1"

    # overwrite=True -> 替换
    changed, prev = set_env_key(target, "XG_FOO", "v3", overwrite=True)
    assert changed is True and prev == "v1"
    assert env_value(target.read_text(encoding="utf-8").splitlines(), "XG_FOO") == "v3"


def test_set_env_key_preserves_other_lines(tmp_path: Path):
    target = tmp_path / ".env"
    target.write_text("XG_A=1\n# keep\nXG_B=2\n", encoding="utf-8")
    set_env_key(target, "XG_A", "9", overwrite=True)
    body = target.read_text(encoding="utf-8")
    assert "XG_A=9" in body and "# keep" in body and "XG_B=2" in body


def test_write_env_atomic_roundtrip(tmp_path: Path):
    target = tmp_path / "nested" / ".env"
    write_env_atomic(target, ["A=1"])
    assert target.read_text(encoding="utf-8") == "A=1\n"