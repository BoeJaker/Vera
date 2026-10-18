"""Patch one JSON record in a Redis LIST without positional races (DEEP-21 D21-12, 12 Oct 2026).

Run lists (`vera:evolve:schedule:runs`, `vera:evolve:runs`) are newest-first lists that writers LPUSH to
while others patch a record by finding its index and LSET-ing it. An LPUSH between the LRANGE and the
LSET shifts every index by one, so the LSET overwrote the NEIGHBOUR record. This does the find-and-set
under WATCH/MULTI: if the list changed in between, the transaction aborts and the find runs again.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

try:
    from redis.exceptions import WatchError
except Exception:                                    # pragma: no cover - redis always installed in prod
    class WatchError(Exception):
        pass


def _load(v: Any) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


async def patch_list_item(r, key: str, match: Callable[[Dict[str, Any]], bool],
                          update: Callable[[Dict[str, Any]], Dict[str, Any]], *,
                          scan: int = 500, retries: int = 6) -> bool:
    """Find the first record in r[key][0:scan] for which match(record), replace it with update(record),
    atomically with respect to other writers. True when a record was patched."""
    for _ in range(max(1, retries)):
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.lrange(key, 0, max(0, scan - 1))
                for i, v in enumerate(raw or []):
                    d = _load(v)
                    if d is None or not match(d):
                        continue
                    new = update(dict(d))
                    pipe.multi()
                    pipe.lset(key, i, json.dumps(new, default=str))
                    await pipe.execute()
                    return True
                await pipe.unwatch()
                return False
            except WatchError:
                continue
    return False
