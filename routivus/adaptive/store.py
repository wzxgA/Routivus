"""adaptive 数据的持久化工具：原子写、损坏回退读取、目录懒创建。

设计依据：red line 为"损坏安全"——任何读失败都回退默认
值，绝不抛错影响启动；写文件用 tmp + os.replace（同目录原子覆盖），
避免写一半导致 JSON 损坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path
from typing import Any

from . import data_dir

# 数据文件名
FEEDBACK_LOG = "feedback.log"        # JSONL 追加，只增不改
CALIBRATION_JSON = "calibration.json"  # 校准结果，原子写
LEARNED_RULES_JSON = "learned_rules.json"  # 自学习规则
ML_ROUTER_BIN = "router.lgb"         # ML 精判产物（joblib 容器）
SEMANTIC_ONNX = "router_semantics.onnx"  # bge 语义编码器（ONNX int8）落盘（见 learned_rules.py）
SEMANTIC_ONNX_TOK = "router_semantics.json"  # 与 .onnx 同名的伴生 tokenizer（semantic.py 按主文件同级取）


def feedback_log_path() -> Path:
    return data_dir() / FEEDBACK_LOG


def calibration_path() -> Path:
    return data_dir() / CALIBRATION_JSON


def learned_rules_path() -> Path:
    return data_dir() / LEARNED_RULES_JSON


def semantic_onnx_path() -> Path:
    return data_dir() / SEMANTIC_ONNX


def reset_adaptive_data() -> list[str]:
    """清空校准与自学习规则（feedback.log 保留作历史）。返回被删除的文件名。

    ``/smartRouter reset``。删除后校准/规则回到空态，
    等价于“删掉 calibration.json + learned_rules.json 回到空态”。
    """
    removed: list[str] = []
    for p in (calibration_path(), learned_rules_path()):
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except OSError:
            pass
    return removed


def ensure_dir() -> Path:
    """懒创建数据目录；重复调用是幂等的。"""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_copy(src: Path, dst: Path) -> None:
    """二进制原子复制：写 tmp 后 os.replace，避免写一半损坏产物。"""
    ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name, suffix=".tmp")
    os.close(fd)
    try:
        import shutil  # noqa: PLC0415
        shutil.copyfile(str(src), tmp)
        os.replace(tmp, dst)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


# 随包默认产物（routivus/assets/，wheel 打包自带）：包内名称 -> 数据目录的目标路径
_BUNDLED_BASENAMES = (ML_ROUTER_BIN, SEMANTIC_ONNX, SEMANTIC_ONNX_TOK)


def _bundled_target(name: str) -> Path:
    """随包产物在数据目录里的对应目标路径（与默认文件名一致）。"""
    return data_dir() / name


def ensure_default_artifacts() -> None:
    """首启把随包产物复制到数据目录；目标已存在则跳过（不覆盖用户数据）。

    随包资源在 ``routivus/assets/``：``router.lgb``（ML 精判兜底）与
    ``router_semantics.onnx`` + ``.json``（语义编码器）。无随包资源、
    目标已存在或复制失败时静默跳过——对应功能维持离线回落，绝不影响启动。
    """
    try:
        from importlib.resources import as_file, files  # noqa: PLC0415
        root = files("routivus").joinpath("assets")
    except Exception:
        return
    for name in _BUNDLED_BASENAMES:
        target = _bundled_target(name)
        if target.exists():
            continue  # 用户已有产物 → 不覆盖
        src = root.joinpath(name)
        if not src.is_file():
            continue
        try:
            with as_file(src) as p_src:
                _atomic_copy(Path(p_src), target)
        except Exception:
            # 内存/沙箱/只读等任何失败都静默，绝不打断启动
            pass


def _atomic_replace(path: Path, payload: str) -> None:
    """写 tmp 文件后 os.replace 覆盖目标，保证同目录内原子替换。"""
    ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        # 清理残留 tmp，避免堆积
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    """把任意 JSON 可序列化对象原子写到 ``path``。"""
    _atomic_replace(path, json.dumps(obj, ensure_ascii=False, indent=2))


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """JSONL 追加写入一行（append 模式，不覆盖既有内容）。"""
    ensure_dir()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")


def read_json_safe(path: Path, default: Any = None) -> Any:
    """读 JSON，文件不存在 / 解析失败 / 结构异常时返回 ``default``，不抛错。"""
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        traceback.print_exc() if os.environ.get("ROUTIVUS_DEBUG") else None
        return default