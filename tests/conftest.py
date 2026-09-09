"""共享测试夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from routivus.config.settings import Settings
from routivus.tool.builtin import build_registry


# 测试用默认自定义 providers：等价于用户在 config.json 中自行定义的一组服务商。
# provider 不再内置，故测试环境同样需从配置加载 provider 定义。
DEFAULT_PROVIDERS: dict[str, dict] = {
    "openai": {"display_name": "OpenAI", "api_base": "https://api.openai.com/v1", "default_model": "gpt-4o-mini"},
    "deepseek": {"display_name": "DeepSeek", "api_base": "https://api.deepseek.com/v1", "default_model": "deepseek-chat"},
    "glm": {"display_name": "GLM", "api_base": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4-flash"},
    "kimi": {"display_name": "Kimi", "api_base": "https://api.moonshot.cn/v1", "default_model": "moonshot-v1-8k"},
}


def seed_config(cfg: dict | None) -> dict:
    """向 config dict 注入默认自定义 providers（已存在时测试显式值优先）。

    按 provider 逐条深合并：默认条目作为底，测试给的同名子键覆盖之（如只改 api_base，
    仍保留默认 default_model），保证 provider 定义完整可解析。
    """
    cfg = dict(cfg or {})
    merged: dict[str, dict] = {name: dict(pdef) for name, pdef in DEFAULT_PROVIDERS.items()}
    for name, pdef in (cfg.get("providers") or {}).items():
        merged[name] = {**merged.get(name, {}), **(pdef or {})}
    cfg["providers"] = merged
    return cfg


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """临时项目目录，预置若干文件。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def main():\n    print('hello')\n", encoding="utf-8"
    )
    (tmp_path / "src" / "util.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def registry(tmp_project: Path) -> object:
    return build_registry(base_dir=tmp_project)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_base="https://api.test/v1",
        api_key="sk-test",
        model="test-model",
        context_window=128_000,
        tool_steps=20,
    )
