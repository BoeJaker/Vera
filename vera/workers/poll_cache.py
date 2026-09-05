"""A value that only changes on upgrade does not need asking every ten seconds.

_fetch_instance_detail polls BOTH /api/version and /api/ps for every Ollama
node on every cluster tick. Measured on prod 2026-09-05 over an exact 120s
window of new log, per node:

    /api/version  33     /api/ps  34     /api/tags  6     /api/embed  0

Identical counts, because they are issued together - about 17 of each per
minute per node, roughly 100 requests a minute across a three-node estate, for
one string that changes when someone upgrades Ollama and at no other time.

/api/ps has to stay on the tick: it reports which models are resident and how
much VRAM they hold, which is the point of the poller. The version does not.

The cache is deliberately invalidated when /api/ps FAILS rather than on a timer
alone, because the one moment a version can realistically change is across a
restart - and a restart is exactly what makes /api/ps stop answering. So a node
that goes away and comes back re-reports its version on the next successful
tick instead of serving a stale string for the rest of the TTL.

Pure: no I/O, no clock of its own.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: How long a cached version is trusted. An hour is far shorter than the real
#: change rate (an Ollama upgrade) and still removes ~360 requests per node.
DEFAULT_TTL = 3600.0


def should_fetch(entry: Optional[Tuple[str, float]], now: float,
                 ttl: float = DEFAULT_TTL) -> bool:
    """Whether the version must be fetched for this node now.

    True when nothing is cached, when the cached value is empty (a previous
    fetch failed and must be retried rather than remembered as ""), or when it
    has aged past `ttl`.
    """
    if not entry:
        return True
    try:
        value, stamp = entry[0], float(entry[1])
    except (TypeError, ValueError, IndexError):
        return True
    if not str(value or "").strip():
        return True
    return (float(now) - stamp) >= float(ttl)


def remember(cache: Dict[str, Any], iid: str, version: str,
             now: float) -> Dict[str, Any]:
    """Record a successful version read. An empty version is NOT cached."""
    if str(version or "").strip():
        cache[str(iid)] = (str(version), float(now))
    return cache


def forget(cache: Dict[str, Any], iid: str) -> Dict[str, Any]:
    """Drop a node's cached version - called when /api/ps fails.

    A node that stopped answering may be restarting, and a restart is the one
    event that can change the version. Forgetting here is what keeps the TTL
    from hiding an upgrade for an hour.
    """
    cache.pop(str(iid), None)
    return cache


def cached_value(entry: Optional[Tuple[str, float]]) -> str:
    """The version to use without refetching, or "" when there is none."""
    if not entry:
        return ""
    try:
        return str(entry[0] or "")
    except (TypeError, IndexError):
        return ""
