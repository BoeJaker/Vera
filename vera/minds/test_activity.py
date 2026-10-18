"""A four-minds test says it is running while it runs (owner, 18 Oct 2026: tests "protected like the censuses are").

`async with declared(id, label): ...` declares a sys.activity of kind `test` - /health shows it to everyone, and the
scheduler holds censuses and exclusive starts while it lasts - and clears it at the end, whatever happens. A missing
or failing activity service never stops the test.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

log = logging.getLogger("vera.minds.test_activity")


def _cap(name: str):
    try:
        from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    except Exception:                    # pragma: no cover - app-free import
        return None
    c = CAPABILITY_REGISTRY.get(name) or {}
    return c.get("raw") or c.get("func")


@asynccontextmanager
async def declared(activity_id: str, label: str, ttl_s: int = 3600) -> AsyncIterator[None]:
    dec, clr = _cap("sys.activity.declare"), _cap("sys.activity.clear")
    if dec:
        try:
            await dec(id=activity_id, kind="test", label=label, by="four-minds", ttl_s=int(ttl_s))
        except Exception as e:
            log.debug("declare %s: %s", activity_id, e)
    try:
        yield
    finally:
        if clr:
            try:
                await clr(id=activity_id)
            except Exception as e:
                log.debug("clear %s: %s", activity_id, e)
