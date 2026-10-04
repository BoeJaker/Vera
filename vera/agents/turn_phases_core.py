"""What a chat turn is doing, said as it happens: phase frames on the chat stream.

Pure (no I/O, no app imports) so it is testable from a worktree as `vera.agents.turn_phases_core`.

One frame shape, agreed between context-and-ui (the throbber and the turn footer) and four-minds (the chat turn
record, which is built from these frames instead of timing the turn a second way), 2026-10-04:

    {type: "phase", turn_id, id, stage, label, state, at_ms, ms?, detail?, count?, sources?}

  stage  sense | needs | acquire | model | post      where in the turn
  state  start | done | skip | fail | late | first   `first` on the model stage = the first token (time to it = TTFT)
  at_ms  milliseconds from the turn's start, on every frame
  ms     the step's duration, on done / skip / fail / late
  label  what the person is told ("searching the web for ..."), short and plain

Frames are additive: a client that does not ask for them (`phase_frames` in the request) never receives any, and
one that does ignores unknown fields.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

STAGES = ("sense", "needs", "acquire", "model", "post")
STATES = ("start", "done", "skip", "fail", "late", "first")
_FINISHED = ("done", "skip", "fail", "late")

# The endpoint's own pre-response timings (`_ep_mark` labels in agents.py) and what each one is called when it is
# shown. A mark not listed here is internal bookkeeping and is not shown.
EP_MARKS: Dict[str, Tuple[str, str, str]] = {
    "agent_lookup":   ("needs",   "agent",  "finding the agent"),
    "context_inject": ("acquire", "notes",  "reading session notes"),
    "canvas_prepare": ("acquire", "canvas", "putting what answers this on the canvas"),
}


def _clean(text: Any, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


class PhaseClock:
    """Builds frames for one turn, timed from the turn's start (`t0`, a monotonic reading)."""

    def __init__(self, turn_id: str, t0: float, clock: Callable[[], float] = time.monotonic):
        self.turn_id = str(turn_id or "")
        self.t0 = float(t0)
        self._clock = clock

    def at_ms(self, t: Optional[float] = None) -> int:
        return max(0, int(round(((self._clock() if t is None else t) - self.t0) * 1000)))

    def frame(self, stage: str, id: str, label: str, state: str, *, ms: Optional[float] = None,
              t: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown phase stage: {stage!r}")
        if state not in STATES:
            raise ValueError(f"unknown phase state: {state!r}")
        if state == "first" and stage != "model":
            raise ValueError("state 'first' belongs to the model stage")
        out: Dict[str, Any] = {"type": "phase", "turn_id": self.turn_id, "id": str(id), "stage": stage,
                               "label": _clean(label), "state": state, "at_ms": self.at_ms(t)}
        if ms is not None and state in _FINISHED:
            out["ms"] = max(0, int(round(ms)))
        for k in ("detail", "count", "sources"):
            v = extra.get(k)
            if v is None or v == "" or v == []:
                continue
            if k == "detail":
                v = _clean(v, 200)
            elif k == "count":
                v = int(v)
            elif k == "sources":
                v = [{"title": _clean(s.get("title"), 140), "url": str(s.get("url") or "")[:500]}
                     for s in list(v)[:10] if isinstance(s, dict)]
            out[k] = v
        return out

    def sse(self, *args: Any, **kwargs: Any) -> bytes:
        """The frame as one server-sent event."""
        return ("data: " + json.dumps(self.frame(*args, **kwargs)) + "\n\n").encode()


def from_marks(clock: PhaseClock, marks: Iterable[Tuple[str, float]], min_ms: int = 1) -> List[Dict[str, Any]]:
    """`done` frames for the steps the endpoint already timed before the stream opened.

    `marks` are (label, seconds since the previous mark), in order, starting at the clock's t0 - the shape agents.py
    keeps in `_ep_marks`. Each shown step gets its true duration and the offset at which it ended; steps shorter
    than `min_ms` are left out (they are noise in a person's view and in the turn record alike)."""
    out: List[Dict[str, Any]] = []
    elapsed = 0.0
    for label, secs in marks:
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            continue
        elapsed += max(0.0, secs)
        spec = EP_MARKS.get(str(label))
        if not spec or secs * 1000 < min_ms:
            continue
        stage, pid, text = spec
        out.append(clock.frame(stage, pid, text, "done", ms=secs * 1000, t=clock.t0 + elapsed))
    return out


def web_label(query: str) -> str:
    q = _clean(query, 60)
    return f"searching the web for “{q}”" if q else "searching the web"
