"""Web Console 的长期记忆视图（只读、失败降级）。

长期记忆由 ``MemoryManager`` 管理，库文件在 ``<项目根>/.routivus/memory.db``，
是**项目级**数据（同一项目的所有会话共享同一份），所以会话快照与 REST 端点都
直接从它读取，不经过 agent 的对话上下文。

设计约束：这是给人看的附加信息，任何异常都不该让快照下发或 HTTP 请求失败——
取不到库、库损坏、条目读取报错，一律降级成 ``status="unavailable"`` 的空视图。
"""

from __future__ import annotations

from typing import Any

__all__ = ["memory_payload"]

# 单次最多回传的条目数（与 TUI `/memory list` 的默认上限一致）。
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _entry_payload(entry: Any) -> dict[str, Any]:
    return {
        "id": int(getattr(entry, "id", 0) or 0),
        "content": str(getattr(entry, "content", "") or ""),
        "source": str(getattr(entry, "source", "") or ""),
        "created_at": _iso(getattr(entry, "created_at", None)),
        "updated_at": _iso(getattr(entry, "updated_at", None)),
    }


def _empty(project_id: str, *, status: str, error: str = "") -> dict[str, Any]:
    return {
        "project_id": project_id,
        "scope": "project",
        "status": status,
        "count": 0,
        "items": [],
        "error": error,
    }


def memory_payload(project_id: str, memory: Any = None, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """把项目长期记忆整成快照 / REST 共用的载荷。

    ``memory`` 为 None 表示"该项目还没有长期记忆（或库打不开）"——这不是错误，
    只是空。读取过程中的异常同样降级为空视图并带上 ``error`` 供界面说明。
    """
    if memory is None:
        return _empty(project_id, status="available")
    capped = max(1, min(int(limit), MAX_LIMIT))
    try:
        items = [_entry_payload(entry) for entry in memory.list(capped)]
    except Exception as exc:  # noqa: BLE001 - 读不到就是没有，不阻断调用方
        return _empty(project_id, status="unavailable", error=str(exc))
    try:
        count = int(memory.count())
    except Exception:  # noqa: BLE001 - 计数失败不影响已读到的条目
        count = len(items)
    return {
        "project_id": project_id,
        "scope": "project",
        "status": "available",
        "count": count,
        "items": items,
        "error": "",
    }
