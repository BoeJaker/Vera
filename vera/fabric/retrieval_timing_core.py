"""Timing envelopes for retrieval (four-minds FM-7.0).

One envelope per retrieval call: each stage (the query embed, each backend, the merge)
records how long it took and how it ended. The outcome is the point: "the vector leg added
nothing" can mean the embed timed out, the breaker was open, Chroma timed out, or Chroma
found nothing, and only the last says anything about relevance.

Outcomes: ok, empty, timeout, error, skipped:<why>.

A process-wide Window keeps the last N envelopes per path and answers p50/p95 per stage
and outcome counts, for the `retrieval.timing` capability.

Pure: no I/O.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# An envelope is worth an event (not just a Window entry) when it is this slow or a
# stage failed; memory.search runs on every chat turn, so every call would flood the bus.
EVENT_SLOW_MS = 8000.0


def leg_outcome(result: Any) -> str:
    """Outcome of one gathered backend result (a list, or the exception gather returned)."""
    if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(result, BaseException):
        return "error"
    return "ok" if result else "empty"


class Envelope:
    """Stages of one retrieval call. `stage()` records one; `mark()` times from the last mark."""

    def __init__(self, path: str, clock=time.monotonic):
        self.path = path
        self._clock = clock
        self._t0 = clock()
        self._last = self._t0
        self.stages: List[Dict[str, Any]] = []

    def mark(self) -> float:
        """ms since the previous mark (or the start); resets the mark."""
        now = self._clock()
        ms = (now - self._last) * 1000.0
        self._last = now
        return ms

    def stage(self, name: str, ms: float, outcome: str, n: Optional[int] = None) -> None:
        row: Dict[str, Any] = {"stage": name, "ms": round(float(ms), 1), "outcome": outcome}
        if n is not None:
            row["n"] = int(n)
        self.stages.append(row)

    @property
    def total_ms(self) -> float:
        return round((self._clock() - self._t0) * 1000.0, 1)

    def worth_an_event(self, slow_ms: float = EVENT_SLOW_MS) -> bool:
        return self.total_ms >= slow_ms or any(
            s["outcome"] in ("timeout", "error") for s in self.stages)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "retrieval.timing", "path": self.path,
                "total_ms": self.total_ms, "stages": list(self.stages)}


async def timed(awaitable) -> Tuple[Any, float]:
    """(result or the exception it raised, ms). CancelledError still propagates."""
    t = time.monotonic()
    try:
        r = await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as e:  # noqa: BLE001 - reported as the leg's outcome
        r = e
    return r, (time.monotonic() - t) * 1000.0


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return round(sorted_vals[i], 1)


def summarize(envs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """p50/p95 of the totals and of each stage, with outcome counts, over some envelopes."""
    totals = sorted(e["total_ms"] for e in envs)
    stages: Dict[str, Dict[str, Any]] = {}
    for e in envs:
        for s in e.get("stages", []):
            st = stages.setdefault(s["stage"], {"ms": [], "outcomes": {}})
            st["ms"].append(s["ms"])
            st["outcomes"][s["outcome"]] = st["outcomes"].get(s["outcome"], 0) + 1
    return {
        "calls": len(envs),
        "total": {"p50": _pct(totals, 0.5), "p95": _pct(totals, 0.95)},
        "stages": {
            name: {"n": len(v["ms"]), "p50": _pct(sorted(v["ms"]), 0.5),
                   "p95": _pct(sorted(v["ms"]), 0.95), "outcomes": v["outcomes"]}
            for name, v in stages.items()},
    }


class Window:
    """The last `size` envelopes per path, and how many each path has ever had (for since())."""

    def __init__(self, size: int = 500):
        self.size = int(size)
        self._by_path: Dict[str, Deque[Dict[str, Any]]] = {}
        self._added: Dict[str, int] = {}

    def add(self, env: Dict[str, Any]) -> None:
        p = env.get("path", "?")
        q = self._by_path.setdefault(p, deque(maxlen=self.size))
        q.append(env)
        self._added[p] = self._added.get(p, 0) + 1

    def marks(self) -> Dict[str, int]:
        """Envelopes added per path so far; pass to since() later."""
        return dict(self._added)

    def summary(self, path: str = "") -> Dict[str, Any]:
        return {p: summarize(list(q)) for p, q in self._by_path.items() if not path or p == path}

    def since(self, marks: Optional[Dict[str, int]]) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Summaries of only the envelopes added after `marks` (FM-7.9: rows that do not
        overlap), and the new marks. Each path's summary also says `added` (calls since the
        marks) - more than `calls` when the window dropped some before they were read."""
        marks = dict(marks or {})
        out: Dict[str, Any] = {}
        for p, q in self._by_path.items():
            new = self._added.get(p, 0) - int(marks.get(p, 0))
            if new <= 0:
                continue
            envs = list(q)[-new:] if new < len(q) else list(q)
            out[p] = dict(summarize(envs), added=new)
        return out, self.marks()
