"""Transport abstraction and common logging helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any


NotificationHandler = Callable[[str, dict], Awaitable[None] | None]
DisconnectHandler = Callable[[Exception], Awaitable[None] | None]


class McpTransport(ABC):
    def __init__(self, *, log_lines: int = 200) -> None:
        self._logs: deque[str] = deque(maxlen=max(1, log_lines))
        self.notification_handler: NotificationHandler | None = None
        self.disconnect_handler: DisconnectHandler | None = None
        self.protocol_version: str | None = None

    def set_protocol_version(self, version: str) -> None:
        self.protocol_version = version

    def set_notification_handler(self, handler: NotificationHandler | None) -> None:
        self.notification_handler = handler

    def set_disconnect_handler(self, handler: DisconnectHandler | None) -> None:
        self.disconnect_handler = handler

    async def dispatch_disconnect(self, error: Exception) -> None:
        handler = self.disconnect_handler
        if handler is None:
            return
        result = handler(error)
        if result is not None:
            await result

    async def dispatch_notification(self, method: str, params: dict | None) -> None:
        handler = self.notification_handler
        if handler is None:
            return
        result = handler(method, params or {})
        if result is not None:
            await result

    def log(self, message: str) -> None:
        self._logs.append(str(message).replace("\r", "").rstrip("\n"))

    def recent_logs(self, limit: int = 50) -> list[str]:
        return list(self._logs)[-max(1, limit):]

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def request(self, method: str, params: dict | None = None) -> dict: ...

    @abstractmethod
    async def notify(self, method: str, params: dict | None = None) -> None: ...

    async def start_notifications(self) -> None:
        """Start an optional server-initiated notification stream."""

    @abstractmethod
    async def close(self) -> None: ...
