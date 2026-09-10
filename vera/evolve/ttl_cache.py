"""A TTL cache that coalesces concurrent callers. Pure asyncio, no I/O.

Written for dashboard endpoints that are POLLED yet cost tens of seconds:
`evolve.authors` (20-35 s: it re-correlates every ingested Claude Code
session with the git log on every call) is refreshed every 20 s by the author
map element and again every 4 s by the Loop Lab's active-run refresh. Each
call took longer than the interval, so calls piled up, the browser's six
connections per origin filled with them, and every other fetch on the page -
the census table, a row expand, a modal - waited minutes behind them
(measured 2026-09-10: census table visible 36 s after the click with no
single request over 1 s).

Two properties matter and both are tested:
  * within `ttl` seconds the stored value is returned without recomputing;
  * concurrent callers during a computation WAIT for the one in flight and
    share its result, instead of each starting their own.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional, Tuple


class TTLCache:
    def __init__(self, ttl_s: float, *, clock: Callable[[], float] = time.monotonic,
                 max_entries: int = 64):
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._max = int(max_entries)
        self._values: Dict[Hashable, Tuple[float, Any]] = {}
        self._locks: Dict[Hashable, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    def peek(self, key: Hashable) -> Optional[Any]:
        ent = self._values.get(key)
        if ent and (self._clock() - ent[0]) < self.ttl_s:
            return ent[1]
        return None

    def invalidate(self, key: Hashable = None) -> None:
        if key is None:
            self._values.clear()
        else:
            self._values.pop(key, None)

    async def get(self, key: Hashable, compute: Callable[[], Awaitable[Any]], *,
                  fresh: bool = False) -> Any:
        if not fresh:
            v = self.peek(key)
            if v is not None:
                self.hits += 1
                return v
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            self.coalesced += 1
        async with lock:
            # Someone may have filled it while we waited on the lock.
            if not fresh:
                v = self.peek(key)
                if v is not None:
                    self.hits += 1
                    return v
            self.misses += 1
            value = await compute()
            if len(self._values) >= self._max:
                oldest = min(self._values, key=lambda k: self._values[k][0])
                self._values.pop(oldest, None)
            self._values[key] = (self._clock(), value)
            return value

    def stats(self) -> Dict[str, Any]:
        return {"ttl_s": self.ttl_s, "entries": len(self._values), "hits": self.hits,
                "misses": self.misses, "coalesced": self.coalesced}
