"""本地样本库：**存语义向量，不存原文**（方案 10 §4.1）。

为什么需要独立一份：``feedback.log`` 只有 ``text_hash + features``（设计如此，保护隐私），
所以纯反馈训练时 TF-IDF 列全零、语义列也不可得（`tools/train_router.py` 的 docstring
早就写明了这个上限）。这里补上缺的那一半——**采集时**把输入的 512 维语义向量算出来、
量化成 int8 落盘，训练时按 ``text_hash`` 与 feedback 记录 join。

隐私：512 维句向量基本不可逆推原文；文件里只有 ``text_hash`` + 向量 + 时间戳，
没有 text 字段（测试里有一条断言专门钉这点）。任何一条写明"清空本地进化数据"的
入口（``/smartRouter reset --hard``）都会删掉它。

体积：int8 × 512 = 512B，base64 后约 700B/条。写入时按 ``MAX_RECORDS`` 滚动截断，
避免无限增长（默认留最近 50000 条 ≈ 35MB 上限）。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .store import append_jsonl, sem_samples_path

MAX_RECORDS = 50_000
_DIM = 512


def _quantize(vec: list[float]) -> str:
    """512 维浮点 → int8 → base64（1/127 精度，对质心/树模型足够）。"""
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(vec[:_DIM], dtype=float)
    if arr.size < _DIM:  # 维度不足补零，保证列宽稳定
        arr = np.concatenate([arr, np.zeros(_DIM - arr.size, dtype=float)])
    q = np.clip(np.round(arr * 127.0), -127, 127).astype("int8")
    return base64.b64encode(q.tobytes()).decode("ascii")


def _dequantize(payload: str) -> list[float] | None:
    import numpy as np  # noqa: PLC0415

    try:
        raw = base64.b64decode(payload)
        arr = np.frombuffer(raw, dtype="int8").astype(float) / 127.0
    except Exception:  # noqa: BLE001 - 损坏记录跳过
        return None
    if arr.size == 0:
        return None
    return arr.tolist()


def write_sem_sample(text_hash: str, sem: list[float] | None,
                     ts: float | None = None, path: Path | None = None) -> bool:
    """追加一条样本；``sem`` 为空（编码器不可用）时不落盘，返回 False。

    只在语义通道可用时才有意义：没有向量就没有"本地语义列"，
    feedback.log 里的 features 已经够训练纯数值模型了。
    """
    if not text_hash or not sem:
        return False
    target = path or sem_samples_path()
    append_jsonl(target, {
        "ts": float(ts if ts is not None else time.time()),
        "text_hash": text_hash,
        "sem": _quantize(sem),
    })
    _trim(target)
    return True


def read_sem_samples(path: Path | None = None) -> dict[str, list[float]]:
    """读回 ``text_hash -> 语义向量``；文件缺失/单行损坏都不抛错。"""
    target = path or sem_samples_path()
    if not target.exists():
        return {}
    out: dict[str, list[float]] = {}
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                h = rec.get("text_hash")
                vec = _dequantize(str(rec.get("sem", "")))
                if h and vec:
                    out[str(h)] = vec
    except OSError:
        return out
    return out


def _trim(path: Path) -> None:
    """超过 ``MAX_RECORDS`` 时保留最近的那批（重写文件，原子替换）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_RECORDS:
        return
    from .store import ensure_dir  # noqa: PLC0415

    ensure_dir()
    keep = "\n".join(lines[-MAX_RECORDS:]) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(keep, encoding="utf-8")
    tmp.replace(path)
