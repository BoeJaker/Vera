"""A default output bound for the agent loop's own generations.

Why (2026-09-24, census run72, goal research-web): one controller call -
job loop_planner, an 18,581-char prompt - produced eval_count = 16384, the
whole window, in 1,055 s on the GPU. Nothing asked for that: a controller
answers with a JSON decision. The operator's thinker has been bounded to
512 tokens since census 35 for the same reason and code.edit has its own
bound; the planner / controller / executor / writer / chat calls that go
through the loop's shared generate wrapper had none, so the window (16k on
the GPU) was the only limit.

Across the ~1,330 loop calls in the six log files before the fix, the
largest legitimate output was 3,407 tokens (executor) and the 95th
percentiles were 330 (planner), 717 (controller), 784 (executor), 1,249
(coder), 1,979 (writer). DEFAULT_LOOP_NUM_PREDICT sits above every one of
them; a caller that pins its own num_predict keeps it.

A pinned num_predict is also what the window fit reads (ctx_policy_core
.output_room), so bounding the output shrinks the window to prompt +
bound + margin instead of reserving the flat 16k on every loop call.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_LOOP_NUM_PREDICT = 4096


def bound_options(options: Optional[Dict[str, Any]], default: int = DEFAULT_LOOP_NUM_PREDICT) -> Dict[str, Any]:
    """`options` with `num_predict` set to `default` unless the caller already
    pinned a positive one. Returns a new dict; never mutates the argument.
    A `default` <= 0 leaves the options exactly as given (the bound is off)."""
    out = dict(options) if options else {}
    try:
        pinned = int(out.get("num_predict") or 0)
    except (TypeError, ValueError):
        pinned = 0
    if pinned > 0:
        return out
    if int(default or 0) > 0:
        out["num_predict"] = int(default)
    else:
        out.pop("num_predict", None)
    return out
