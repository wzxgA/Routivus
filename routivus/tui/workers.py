"""Async task helpers kept separate from widget code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def start(coro_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
    """Start a controller coroutine on the current event loop."""
    return asyncio.create_task(coro_factory())
