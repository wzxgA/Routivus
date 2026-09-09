"""SmartRouter 自适应反馈子系统。

负责把用户的隐式行为信号（interrupt / clarify / cmd_retry / short_high_tier）
落盘到 ``~/.routivus/adaptive/``，供后续聚合校准。

数据目录沿用项目用户级配置约定（``~/.routivus``），env 变量可覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

# adaptive 数据目录的 env 覆盖键（可选，不设时用默认 ~/.routivus/adaptive）
ADAPTIVE_DIR_ENV = "ROUTIVUS_ADAPTIVE_DIR"


def data_dir() -> Path:
    """解析 adaptive 数据目录：env ``ROUTIVUS_ADAPTIVE_DIR`` 覆盖，默认 ``~/.routivus/adaptive``。

    仅做路径解析，不创建目录；目录懒创建由 store 在写入时处理。
    """
    override = os.environ.get(ADAPTIVE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".routivus" / "adaptive"