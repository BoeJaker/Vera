"""Which node gets the next request when several are equally idle.

The orchestrator's own `pick_instance` learned this in August: a stream of
one-at-a-time requests - embeddings above all - sees `in_use == 0` on every
node every time, so every key ties and `min` returns the same node forever.
Measured then: 8,541 of 8,541 embeddings, 100%, on one CPU node while its peer
sat idle. The answer was a least-recently-used tie-break (`_LAST_PICKED`).

It did not work, and `vera/workers/cluster.py` - which REPLACES pick_instance -
did not even carry it. Embeddings per census, cpu-246 / cpu-247:

    run59  151 / 63      run60  318 / 22      run61  235 / 21

Still 90/10, because **the static `priority` field is compared BEFORE fairness
in both routers**, and the two nodes have different priorities (1 and 2). Two
idle nodes therefore never tie, the tie-break never runs, and cpu-246 wins
every single time. A burst of twenty embeds queues on one node at ~5s each
instead of splitting across two.

So the order here is: real LOAD first, then any deliberate soft preference,
then FAIRNESS, and only then static priority. Priority still decides between
two nodes that are equally loaded AND were last used at the same instant,
which in practice means it orders a cold estate on its first request and stops
being a monopoly after that.

Pure: rankings in, one id out.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def choose(rank: Mapping[str, Any],
           last_picked: Optional[Mapping[str, float]] = None,
           after: Optional[Mapping[str, Any]] = None) -> str:
    """The best id in `rank`, with fairness breaking ties.

    `rank`     - everything that OUTRANKS fairness (load, soft preference).
                 Lowest wins. A float or a tuple, as long as all values are
                 the same shape.
    `after`    - what breaks a tie that survives fairness (static priority).
    `last_picked` - id -> the clock reading when it last got work. A node that
                 has never been picked sorts first, so a cold peer is tried
                 before a warm one.

    The id itself is the final term, so the choice is deterministic when
    nothing else separates two nodes.
    """
    if not rank:
        return ""
    lp = last_picked or {}
    af = after or {}
    # A node with no stamp has been waiting since before the record began, so
    # it sorts ahead of anything already used. Defaulting to 0.0 instead made
    # a fresh peer merely TIE with a node picked at t=0, and the pick fell
    # back to priority - which handed the first two requests to the same node.
    return min(rank, key=lambda k: (rank[k], float(lp.get(k, float("-inf"))),
                                    af.get(k, 0), str(k)))


def record(last_picked: Optional[Dict[str, float]], chosen: str,
           now: float) -> Dict[str, float]:
    """Note that `chosen` has just been handed work. Returns the mapping.

    Mutates in place when given a real dict, so a caller sharing the
    orchestrator's own `_LAST_PICKED` keeps one record across both routers.
    """
    lp = last_picked if isinstance(last_picked, dict) else {}
    if chosen:
        lp[str(chosen)] = float(now)
    return lp
