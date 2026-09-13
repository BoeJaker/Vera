"""Small asyncio single-flight primitive for expensive controller operations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class SingleFlight:
    """Share one task per stable key, including across caller cancellation.

    Shielding is essential for HTTP callers: losing the response must not cancel
    ownership tracking while the child process continues on the host.
    """

    def __init__(self, retention_seconds: float = 600.0, maximum: int = 64):
        self._retention = max(1.0, float(retention_seconds))
        self._maximum = max(1, int(maximum))
        self._tasks: dict[str, tuple[asyncio.Task, float]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (task, created) in list(self._tasks.items()):
            if task.done() and now - created >= self._retention:
                self._tasks.pop(key, None)
        if len(self._tasks) <= self._maximum:
            return
        completed = sorted(((created, key) for key, (task, created) in self._tasks.items()
                            if task.done()))
        for _, key in completed[:max(0, len(self._tasks) - self._maximum)]:
            self._tasks.pop(key, None)

    async def run(self, key: str, factory: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        self._prune()
        record = self._tasks.get(key)
        shared = record is not None
        if record is None:
            task = asyncio.create_task(factory())
            self._tasks[key] = (task, time.monotonic())
        else:
            task = record[0]
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The task deliberately remains discoverable for the retrying
            # caller; shield prevents cancellation reaching the host command.
            raise
        except BaseException:
            if self._tasks.get(key, (None,))[0] is task:
                self._tasks.pop(key, None)
            raise
        if self._tasks.get(key, (None,))[0] is task:
            self._tasks.pop(key, None)
        return result, shared
