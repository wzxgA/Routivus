"""adaptive 存储层单测。"""

from __future__ import annotations

import json

import pytest

from routivus.adaptive import data_dir
from routivus.adaptive import store


@pytest.fixture()
def adapt_dir(monkeypatch, tmp_path):
    """把 ROUTIVUS_ADAPTIVE_DIR 指到临时目录，隔离磁盘读写。"""
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(tmp_path))
    return tmp_path


def test_data_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("ROUTIVUS_ADAPTIVE_DIR", raising=False)
    d = data_dir()
    assert ".routivus" in str(d)
    assert d.name == "adaptive"


def test_data_dir_env_override(adapt_dir):
    assert data_dir() == adapt_dir


def test_ensure_dir_creates_lazily(monkeypatch, tmp_path):
    # 用 ROUTIVUS_ADAPTIVE_DIR 指向一个尚不存在的子目录，验证 ensure_dir 懒创建
    target = tmp_path / "data"
    monkeypatch.setenv("ROUTIVUS_ADAPTIVE_DIR", str(target))
    assert not target.exists()
    store.ensure_dir()
    assert target.is_dir()


def test_ensure_dir_idempotent(adapt_dir):
    store.ensure_dir()
    store.ensure_dir()  # 不应抛错
    assert adapt_dir.is_dir()


def test_path_constants(adapt_dir):
    store.ensure_dir()
    assert store.feedback_log_path().name == "feedback.log"
    assert store.calibration_path().name == "calibration.json"
    assert store.learned_rules_path().name == "learned_rules.json"


def test_atomic_write_json_creates_file(adapt_dir):
    p = adapt_dir / "calibration.json"
    store.atomic_write_json(p, {"bias": {"Enhanced": 0.1}})
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"bias": {"Enhanced": 0.1}}


def test_atomic_write_json_overwrites(adapt_dir):
    p = adapt_dir / "calibration.json"
    store.atomic_write_json(p, {"v": 1})
    store.atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}


def test_append_jsonl_appends_not_overwrites(adapt_dir):
    p = adapt_dir / "feedback.log"
    store.append_jsonl(p, {"a": 1})
    store.append_jsonl(p, {"a": 2})
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"a": 2}


def test_read_json_safe_missing_returns_default(adapt_dir):
    assert store.read_json_safe(adapt_dir / "nope.json", default={"x": 1}) == {"x": 1}


def test_read_json_safe_corrupt_returns_default(adapt_dir):
    p = adapt_dir / "corrupt.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert store.read_json_safe(p, default="fallback") == "fallback"


def test_read_json_safe_loads_valid(adapt_dir):
    p = adapt_dir / "ok.json"
    store.atomic_write_json(p, {"k": "v"})
    assert store.read_json_safe(p) == {"k": "v"}