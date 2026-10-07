"""The retrieval eval set from implicit labels (four-minds FM-7.0).

Every memory.seek is a ranking decision on the decision ledger (question `memory.seek`, the
answer is the ranked record ids). When the same process then reads one of those records with
memory.read within RECENT_WINDOW_S, that is an outcome on the seek: "used", at the rank it
held. Mining this after the fact did not work: the loop's event log keeps neither the seek's
arguments nor more than the first 600 characters of its result (2026-10-06: 82 seeks, 85 reads,
0 matchable).

Only positives come from reads; a seek with no read says nothing. So the summary reports use
rate, hit@1 / hit@3 and MRR over the seeks that were used, not precision against a full truth set.

Pure: no I/O.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

SEEK_Q = "memory.seek"
RECENT_WINDOW_S = 1800.0
MAX_IDS = 25


def seek_decision(query: str, ids: List[str], *, origin: str = "user", minds_digest: str = "default",
                  seek_id: str = "") -> Dict[str, Any]:
    ids = [str(i)[:80] for i in (ids or [])][:MAX_IDS]
    return LC.make_decision(
        SEEK_Q, ids, origin=origin, surface="memory", qtype="ranking",
        labels=[], state={"q": str(query or "")}, state_ref={"kind": "seek", "id": seek_id or LC.new_decision_id()[4:]},
        decider="rule", implementation="memory.seek:rrf+floor+mmr", mode="rule",
        minds_digest=minds_digest or "default", detail={"k": len(ids)})


def read_outcome(decision_id: str, rank: int) -> Dict[str, Any]:
    return LC.make_outcome(decision_id, "used", "seek.read", "medium", detail={"rank": int(rank)})


class RecentSeeks:
    """The process's last seeks, so a read can be credited to the seek that surfaced the record."""

    def __init__(self, size: int = 200, window_s: float = RECENT_WINDOW_S, clock=time.monotonic):
        self._q: Deque[Tuple[float, str, List[str], set]] = deque(maxlen=size)
        self.window_s = float(window_s)
        self._clock = clock

    def add(self, decision_id: str, ids: Iterable[str]) -> None:
        self._q.append((self._clock(), decision_id, [str(i) for i in ids], set()))

    def match(self, record_id: str) -> Optional[Tuple[str, int]]:
        """(decision_id, 1-based rank) of the newest seek in the window that ranked this record and
        has not been credited for it yet; None otherwise. Marks it credited."""
        rid = str(record_id or "")
        now = self._clock()
        for ts, did, ids, used in reversed(self._q):
            if now - ts > self.window_s:
                break
            if rid in ids and rid not in used:
                used.add(rid)
                return did, ids.index(rid) + 1
        return None


def pick_seeks(entries: Iterable[Dict[str, Any]], record_id: str, now: float,
               window_s: float = RECENT_WINDOW_S) -> List[Tuple[str, int]]:
    """From shared recent-seek entries ({"did", "ids", "ts"}, newest first), the seeks in the window
    that ranked this record, newest first, as (decision_id, 1-based rank)."""
    rid = str(record_id or "")
    out: List[Tuple[str, int]] = []
    for e in entries or []:
        try:
            ts = float(e.get("ts") or 0)
            ids = [str(i) for i in e.get("ids") or []]
        except Exception:
            continue
        if now - ts > window_s:
            continue
        if rid in ids:
            out.append((str(e.get("did")), ids.index(rid) + 1))
    return out


def summarize(seeks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """seeks: [{"decision_id", "k", "used_ranks": [int]}]. Use rate, hit@1, hit@3 and MRR over used seeks."""
    n = len(seeks)
    used = [s for s in seeks if s.get("used_ranks")]
    firsts = [min(s["used_ranks"]) for s in used]
    m = len(used)
    return {"seeks": n, "used": m, "use_rate": round(m / n, 3) if n else None,
            "hit@1": round(sum(1 for r in firsts if r <= 1) / m, 3) if m else None,
            "hit@3": round(sum(1 for r in firsts if r <= 3) / m, 3) if m else None,
            "mrr": round(sum(1.0 / r for r in firsts) / m, 3) if m else None}
