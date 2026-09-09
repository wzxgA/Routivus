"""记忆层数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class MemoryEntry:
    id: int
    content: str
    source: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SharedSection:
    """发给模型的静态上下文片段。"""

    source: str
    text: str


@dataclass(frozen=True)
class CompressionResult:
    status: Literal["ready", "compacted", "warning", "error", "overflow"]
    before_tokens: int
    after_tokens: int
    compressed_turns: int = 0
    message: str = ""
    warnings: tuple[str, ...] = ()
    request_token_limit: int = 0

    @property
    def proceed(self) -> bool:
        return self.status in ("ready", "compacted", "warning", "error") and (
            self.status != "overflow"
        )
