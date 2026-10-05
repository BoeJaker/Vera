"""The order gates start in: oldest pipeline first, across every process. Pure.

4 Oct 2026: the critical-tier lease went to whichever waiter polled first after it freed, the
test pool took gates in arrival order, and a drained release let every held gate race - a gate
adopted a minute ago could start before one queued for an hour. Gates now wait in one Redis
line ordered by their pipeline's created_at; a gate may start only when no older, live gate is
waiting ahead of it. A waiter's heartbeat lapsing (its process died) drops it from the line.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

QUEUE_KEY = "vera:evolve:critical_tier:line"        # zset token -> created_at epoch
HEARTBEAT_KEY = "vera:evolve:critical_tier:line:hb"  # hash token -> last seen epoch
HEARTBEAT_TTL_S = 60.0


def score(created_at: Any, now: float) -> float:
    """The pipeline's creation time as epoch seconds; `now` when it is missing or bad."""
    try:
        d = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return float(now)


def _s(x: Any) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def ahead(line: Iterable[Tuple[Any, float]], heartbeats: Dict[str, Any], token: str,
          now: float, ttl: float = HEARTBEAT_TTL_S) -> Tuple[List[str], List[str]]:
    """(alive gates ahead of `token`, stale gates to drop). `line` is the zset as
    (member, score) oldest first; a member whose heartbeat is older than ttl is stale."""
    alive_ahead: List[str] = []
    stale: List[str] = []
    for member, _sc in line or []:
        m = _s(member)
        if m == token:
            break
        try:
            seen = float(_s(heartbeats.get(m)))
        except (TypeError, ValueError):
            seen = 0.0
        if now - seen > ttl:
            stale.append(m)
        else:
            alive_ahead.append(m)
    return alive_ahead, stale
