"""隐式反馈信号：枚举、事件结构、内存缓冲 + 定时 flush 到 feedback.log（JSONL）。

设计依据：/B。
- A 步骤交付骨架：SignalType 枚举、FeedbackEvent、FeedbackRecorder 的
  内存缓冲与 flush、read_feedback 读取。
- B 步骤才接入真实采集挂点；在此之前的 recorder 不会产生任何写入
  （缓冲区为空时 flush 是 no-op）。

信号清单（四类可采集 + file_revert 预留位，本期不采集 file_revert）：
- interrupt     用户在回答中途 Ctrl+C（upgrade，weight 0.9）
- clarify       上一轮后紧接追问/否定，且上轮档 ≤Superior（upgrade，weight 1.0）
- cmd_retry     紧接重试类输入（upgrade，weight 0.6）
- short_high_tier 简短闲聊却落到中高档（downgrade，weight 0.3）
"""

from __future__ import annotations

import enum
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .store import append_jsonl, ensure_dir, feedback_log_path

DEFAULT_FLUSH_INTERVAL = 10.0  # 秒，缓冲不足或未到周期时不下盘


class SignalType(str, enum.Enum):
    INTERRUPT = "interrupt"
    CLARIFY = "clarify"
    CMD_RETRY = "cmd_retry"
    SHORT_HIGH_TIER = "short_high_tier"
    FILE_REVERT = "file_revert"  # 预留占位：本期不采集


# 各信号的 vector：upgrade=True 表示该升档，False 表示该降档；weight 表示可信度
SIGNAL_META: dict[SignalType, dict[str, Any]] = {
    SignalType.INTERRUPT: {"upgrade": True, "weight": 0.9},
    SignalType.CLARIFY: {"upgrade": True, "weight": 1.0},
    SignalType.CMD_RETRY: {"upgrade": True, "weight": 0.6},
    SignalType.SHORT_HIGH_TIER: {"upgrade": False, "weight": 0.3},
    SignalType.FILE_REVERT: {"upgrade": False, "weight": 0.5},  # 预留，未实现
}


def text_hash(text: str) -> str:
    """对输入文本取短哈希，用于对账/去重，不存原文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class FeedbackEvent:
    """一条反馈记录，对应 feedback.log 的一行 JSON。"""

    source: SignalType
    model_tier: str  # 触发信号时路出的档位名（如 "Superior"）
    session: str  # 当前项目目录标识（可空，如无项目）
    text_hash_val: str = ""
    signal: str = ""  # 冗余命中类型名，聚合时用
    weight: float = 0.0
    ts: float = field(default_factory=time.time)
    features: dict | None = None  # 当轮特征快照（learned_rules 聚合用）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ts": self.ts,
            "session": self.session,
            "source": self.source.value,
            "signal": "upgrade" if self.signal == "upgrade" else
                      ("downgrade" if self.signal == "downgrade" else ""),
            "text_hash": self.text_hash_val,
            "model_tier": self.model_tier,
            "weight": self.weight,
        }
        if self.features:  # 仅当存在时落盘，向后兼容（旧记录无此字段）
            d["features"] = self.features
        return d


class FeedbackRecorder:
    """内存缓冲 + 定时 flush 的反馈记录器。

    用法：
        rec = FeedbackRecorder(session="proj-abc")
        rec.capture(SignalType.CLARIFY, model_tier="Superior")
        # 定期调用 rec.flush()，或依赖 __del__ / 显式 flush
    """

    def __init__(
        self,
        session: str = "",
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        log_path: Path | None = None,
    ) -> None:
        self.session = session
        self.flush_interval = flush_interval
        self._buffer: list[FeedbackEvent] = []
        self._path = log_path or feedback_log_path()

    def capture(
        self,
        source: SignalType,
        model_tier: str,
        text: str = "",
        ts: float | None = None,
        features: dict | None = None,
    ) -> None:
        """入内存缓冲（不立即写盘）。"""
        meta = SIGNAL_META[source]
        ev = FeedbackEvent(
            source=source,
            model_tier=model_tier,
            session=self.session,
            text_hash_val=text_hash(text) if text else "",
            signal="upgrade" if meta["upgrade"] else "downgrade",
            weight=meta["weight"],
            ts=ts if ts is not None else time.time(),
            features=features,
        )
        self._buffer.append(ev)

    def flush(self, force: bool = False) -> int:
        """把缓冲逐行追加写入 feedback.log，返回写入条数。

        仅当 buffer 非空时写盘；返回值为本次追加的行数。
        force 参数保留（语义：即便 buffered 条数不足也写），A 步骤无定时器，
        调用方负责按 flush_interval 控制节奏。
        """
        if not self._buffer:
            return 0
        ensure_dir()
        n = 0
        for ev in self._buffer:
            append_jsonl(self._path, ev.to_dict())
            n += 1
        self._buffer.clear()
        return n

    def count(self) -> int:
        """当前缓冲区未 flush 的条数。"""
        return len(self._buffer)


def read_feedback(path: Path | None = None) -> list[dict[str, Any]]:
    """读取 feedback.log 全部记录；文件不存在返回空列表，单行损坏跳过不抛错。"""
    path = path or feedback_log_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(_json_loads(line))
                except Exception:
                    continue  # 单行损坏跳过，不拖垮整体
    except OSError:
        return []
    return records


def _json_loads(line: str) -> dict[str, Any]:
    import json

    return json.loads(line)