"""Progress steps for long-running capabilities.

`emit_step` sends one ``<cap>.step`` event through the capability event bus so
the UI can show where a run has got to ("probing 40/254, 3 found").

Rules:
  * never raises - a failed emit is swallowed and logged at debug;
  * does nothing without a trace_id (steps belong to a run);
  * throttled per (cap, trace_id) to one event per ``THROTTLE_S`` seconds,
    except when the stage changes or done == total (the last one always goes).
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

THROTTLE_S = 0.25
MAX_TRACKED = 512

# (cap, trace_id) -> (last emit monotonic time, last stage)
_last: "OrderedDict[Tuple[str, str], Tuple[float, str]]" = OrderedDict()

# Indirection so tests can drive a fake clock.
_clock = time.monotonic


def _is_final(done: Optional[int], total: Optional[int]) -> bool:
    return done is not None and total is not None and done >= total


async def emit_step(cap: str, trace_id, stage: str, *,
                    done: Optional[int] = None, total: Optional[int] = None,
                    text: str = "", **extra: Any) -> None:
    """Emit one ``{cap}.step`` progress event (throttled, never raises)."""
    try:
        if not trace_id:
            return
        key = (str(cap), str(trace_id))
        now = _clock()
        final = _is_final(done, total)
        prev = _last.get(key)
        if prev is not None and not final:
            last_t, last_stage = prev
            if last_stage == stage and (now - last_t) < THROTTLE_S:
                return
        if final:
            _last.pop(key, None)
        else:
            _last[key] = (now, stage)
            _last.move_to_end(key)
            while len(_last) > MAX_TRACKED:
                _last.popitem(last=False)

        ev = {
            "type": f"{cap}.step",
            "cap": cap,
            "trace_id": trace_id,
            "stage": stage,
            "done": done,
            "total": total,
            "text": (text or "")[:300],
            "ts": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        ev = {k: v for k, v in ev.items() if v is not None}

        from Vera.vera.capability_orchestration import emit_event
        await emit_event(ev)
    except Exception as e:  # never let progress reporting break a run
        log.debug("emit_step %s/%s failed: %s", cap, stage, e)
