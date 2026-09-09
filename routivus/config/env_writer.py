"""幂等读写本地 :file:`.env` 文件（用于交互式写入 API Key）。

设计口径（对齐 02 方案 F2 / J2）：
- config.json 永不落 key；key 只经 :file:`.env` 的 ``XG_<NAME>_API_KEY`` 提供。
- 本模块只负责「定位、增改、去重、原子写」某一行，不做其他配置管理。
- 写入对原文幂等：已存在 -> 原位保留注释风格并替换值；不存在 -> 追加。
- 用临时文件 + ``os.replace`` 原子落盘，失败不破坏原文件。

定位规则（J2）：从工作目录向上查找命中现有的 :file:`.env`（最多 3 层）；
找不到时回退到 ``fallback_dir / ".env"``。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def find_env_file(start_dir: str | Path | None = None, max_up: int = 3) -> Path | None:
    """从 ``start_dir`` 向上查找已存在的 :file:`.env`（最多 ``max_up`` 层）。"""
    cur = Path(start_dir) if start_dir else Path.cwd()
    for _ in range(max_up):
        candidate = cur / ".env"
        if candidate.is_file():
            return candidate
        if cur == cur.parent:
            break
        cur = cur.parent
    return None


def decide_env_path(start_dir: str | Path | None = None, fallback_dir: str | Path | None = None) -> Path:
    """确定要写入的 .env 路径：优先命中现有文件，否则落到 fallback 目录。"""
    existing = find_env_file(start_dir)
    if existing is not None:
        return existing
    base = Path(fallback_dir) if fallback_dir else Path.cwd()
    return base / ".env"


def env_value(lines: list[str], key: str) -> str | None:
    """取 ``key`` 当前生效值（容忍 ``KEY=v`` / ``KEY = v``，返回去引号值）。"""
    prefix = f"{key}="
    for raw in lines:
        line = raw.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"').strip("'")
        if line.startswith(f"{key} ="):
            return line[len(f"{key} ="):].strip().strip('"').strip("'")
    return None


def upsert_env_key(lines: list[str], key: str, value: str) -> list[str]:
    """返回把 ``key=value`` 写入后的新行列表（已存在则替换，否则追加）。

    - 只匹配精确 ``KEY=...`` 行；注释行、导出行不变。
    - 存在重复行时保留第一处位置并去重后续重复。
    """
    prefix = f"{key}="
    key_line = f"{key}={value}"
    out: list[str] = []
    replaced = False
    for raw in lines:
        line = raw.strip()
        if not replaced and line.startswith(prefix):
            # 保留该行缩进/前缀风格：追加新赋值行
            spaces = raw[: len(raw) - len(raw.lstrip())]
            out.append(f"{spaces}{key_line}")
            replaced = True
            continue
        if line.startswith(prefix):
            # 已存在 -> 去重后续重复行
            continue
        out.append(raw)
    if not replaced:
        out.append(key_line)
    return out


def write_env_atomic(path: str | Path, lines: list[str]) -> None:
    """把 ``lines`` 原子写入 ``path``（临时文件 + reinstated替换）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line if line.endswith("\n") else line + "\n")
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def set_env_key(
    path: str | Path,
    key: str,
    value: str,
    *,
    overwrite: bool = False,
) -> tuple[bool, str | None]:
    """写入 ``key=value`` 到 ``path``。

    返回 ``(changed, previous)``：
    - ``key`` 不存在 -> 追加，返回 ``(True, None)``；
    - ``key`` 已存在且 ``overwrite=True`` -> 替换，返回 ``(True, 旧值)``；
    - ``key`` 已存在且 ``overwrite=False`` -> 不做修改，返回 ``(False, 旧值)``。
    由调用方负责占位值拦截与确认，本函数只做机械写入（幂等原则）。
    """
    target = Path(path)
    original = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    previous = env_value(original, key)
    if previous is not None and not overwrite:
        return False, previous
    next_lines = upsert_env_key(original, key, value)
    write_env_atomic(target, next_lines)
    return True, previous