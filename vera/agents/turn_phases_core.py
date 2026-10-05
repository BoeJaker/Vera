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
MAX_LOG = 60          # frames kept per turn for its record
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

    def __init__(self, turn_id: str, t0: float, clock: Callable[[], float] = time.monotonic,
                 silent: bool = False):
        self.turn_id = str(turn_id or "")
        self.t0 = float(t0)
        self._clock = clock
        # Every frame is kept for the turn record (four-minds FM-0.3), whether or not the client
        # asked to see frames; a silent clock sends nothing (sse/raw give b"").
        self.silent = bool(silent)
        self.log: List[Dict[str, Any]] = []

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
        if len(self.log) < MAX_LOG:
            self.log.append(out)
        return out

    def sse(self, *args: Any, **kwargs: Any) -> bytes:
        """The frame as one server-sent event (b"" on a silent clock - the frame is still logged)."""
        f = self.frame(*args, **kwargs)
        return b"" if self.silent else ("data: " + json.dumps(f) + "\n\n").encode()

    def raw(self, frame: Dict[str, Any]) -> bytes:
        """An already-built frame as one server-sent event (b"" on a silent clock)."""
        return b"" if self.silent else ("data: " + json.dumps(frame) + "\n\n").encode()


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


def turn_record(frames: Iterable[Dict[str, Any]], *, agent: str, elapsed_ms: int, chars: int = 0,
                ok: bool = True) -> Dict[str, Any]:
    """One chat turn, from its phase frames (four-minds FM-0.3, ROADMAP §5.2): no second timer.

    ttft_ms   the model stage's `first` frame (None when no token came);
    kind      "triggered" when the turn acquired web or deep-memory context before the model
              (those turns may wait up to their caps, ROADMAP §13a) - reported apart, so they
              can't hide in the plain turns' average;
    phases    each finished step: stage, id, state, at_ms, ms."""
    frames = list(frames or [])
    ttft = next((f.get("at_ms") for f in frames if f.get("stage") == "model" and f.get("state") == "first"), None)
    triggered = any(f.get("stage") == "acquire" and f.get("id") in ("web", "memory")
                    and f.get("state") in ("start", "done", "late", "fail") for f in frames)
    phases = [{k: f.get(k) for k in ("stage", "id", "state", "at_ms", "ms") if f.get(k) is not None}
              for f in frames if f.get("state") in _FINISHED or f.get("state") == "first"]
    return {"type": "chat.turn", "turn_id": next((f.get("turn_id") for f in frames if f.get("turn_id")), ""),
            "agent": _clean(agent, 60), "kind": "triggered" if triggered else "plain",
            "ttft_ms": ttft, "elapsed_ms": int(elapsed_ms), "chars": int(chars or 0), "ok": bool(ok),
            "phases": phases[:MAX_LOG]}


def as_envelope(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record in retrieval_timing_core's Window shape: path = "<agent>|<kind>", stages = ttft + phases."""
    stages = []
    if record.get("ttft_ms") is not None:
        stages.append({"stage": "ttft", "ms": float(record["ttft_ms"]), "outcome": "ok"})
    else:
        stages.append({"stage": "ttft", "ms": float(record.get("elapsed_ms") or 0), "outcome": "no-token"})
    for p in record.get("phases") or []:
        if p.get("ms") is not None:
            stages.append({"stage": f"{p.get('stage')}.{p.get('id')}", "ms": float(p["ms"]),
                           "outcome": str(p.get("state"))})
    return {"path": f"{record.get('agent')}|{record.get('kind')}", "total_ms": float(record.get("elapsed_ms") or 0),
            "stages": stages}
