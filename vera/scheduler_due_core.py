"""When a registered periodic job is due. Pure.

`schedule(fn, interval=0)` was meant as "start this once - it loops by itself". The loop
in `scheduler_loop` treated 0 as "every tick": `(now - last) >= 0` is always true, so it
created a NEW task every second. vllm's `_health_loop` (a `while True` loop) was the one
job registered that way: prod held ~2,950 copies 50 minutes after a restart (4 Oct 2026,
sys.debug.tasks) - ~3,600 an hour, every one waking each minute, for the life of the process.
A job with interval <= 0 now starts exactly once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def due(last: Optional[datetime], interval: float, now: datetime) -> bool:
    """Should the job start now? interval <= 0 = start once (it runs its own loop)."""
    try:
        iv = float(interval)
    except (TypeError, ValueError):
        iv = 0.0
    if last is None:
        return True
    if iv <= 0:
        return False
    return (now - last).total_seconds() >= iv
