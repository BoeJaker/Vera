"""
cap_jobs_core.py — pure logic for running a capability as idle-queue work
=========================================================================

No app, no Redis, no asyncio: the decisions here are the ones worth testing
without booting Vera (`tests/test_cap_jobs_core.py` imports this as
`vera.background.cap_jobs_core`).

The queue already exists (`vera/idle_queue.py`) and so does the general job
system (`dispatch_task` → the `vera:tasks` stream → a worker → `vera:results`).
What was missing was a bridge: the queue could only run six built-in kinds,
so an automation that wanted "call this capability, but only when nobody is
using the box" had no way to say it. This module is that bridge, and the
functions below decide what may be submitted, how a job is labelled, and how
long it may run — the parts a bad answer to would either run something it
should not, or hang the queue.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

#: The queue kind. One word: it is the only kind whose payload names a cap.
KIND_CAP = "cap"

#: Capabilities that must never run from the idle queue. The queue runs
#: unattended and its jobs can be submitted over an unauthenticated LAN call,
#: so anything that restarts, promotes, or manipulates the queue itself is
#: refused here rather than left to judgement at run time.
DENY_PREFIXES: Tuple[str, ...] = (
    "sys.",
    "background.",
    "evolve.bleeding_edge.",
    "evolve.pipeline.promote",
    "evolve.sandbox.down",
    "cluster.job.stop",
    "jobs.purge_pending",
    "census.control",
)

#: Default wall-clock ceiling for one job, and the hard maximum a submitter
#: may ask for. An LLM call is unbounded under contention, so the default is
#: generous; the maximum keeps one bad job from holding the queue all night.
DEFAULT_TIMEOUT_S = 900.0
MAX_TIMEOUT_S = 3600.0
MIN_TIMEOUT_S = 10.0

#: How long a finished job's result stays fetchable.
RESULT_TTL_S = 48 * 3600

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


def is_denied(name: str) -> bool:
    n = (name or "").strip().lower()
    return any(n.startswith(p) for p in DENY_PREFIXES)


def validate_request(name: str, arguments: Any, *, known: Optional[set] = None
                     ) -> Tuple[bool, str]:
    """(ok, reason). `known` is the live capability registry's key set."""
    n = (name or "").strip()
    if not n:
        return False, "name is required"
    if not _NAME_RE.match(n):
        return False, f"not a capability name: {n!r}"
    if is_denied(n):
        return False, f"{n} may not run from the idle queue"
    if known is not None and n not in known:
        return False, f"unknown capability: {n}"
    if arguments is not None and not isinstance(arguments, dict):
        return False, "arguments must be an object"
    if isinstance(arguments, dict) and "trace_id" in arguments:
        return False, "arguments may not carry trace_id"
    return True, ""


def clamp_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if t <= 0:
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, t))


def bg_label(name: str, job_id: str) -> str:
    """The BACKGROUND_LLM label the worker sets while running this cap.

    Everything an Ollama request logs about its origin comes from this label,
    so it names both the capability and the job.
    """
    return f"cap:{(name or '').strip()}:{str(job_id or '')[:12]}"


def job_title(name: str, title: str = "") -> str:
    t = (title or "").strip()
    return t if t else f"cap {name}"


def result_key(job_id: str) -> str:
    return f"vera:background:cap:{job_id}"


def build_payload(name: str, arguments: Optional[Dict[str, Any]],
                  timeout_s: Any = None, submitted_by: str = "") -> Dict[str, Any]:
    return {
        "name": (name or "").strip(),
        "arguments": dict(arguments or {}),
        "timeout_s": clamp_timeout(timeout_s),
        "submitted_by": (submitted_by or "").strip()[:80],
    }


def summarise_result(result: Any, limit: int = 160) -> str:
    """One line for the queue's own note field."""
    if isinstance(result, dict):
        if result.get("error"):
            return ("error: " + str(result["error"]))[:limit]
        for k in ("summary", "text", "message", "status"):
            if result.get(k):
                return str(result[k]).replace("\n", " ")[:limit]
        return ("ok: " + ", ".join(sorted(result.keys())[:6]))[:limit]
    return str(result)[:limit]


def outcome(result: Any) -> str:
    """done | failed | timeout | cancelled — what the job record should say."""
    if isinstance(result, dict):
        err = str(result.get("error") or "")
        if not err:
            return "done"
        if err == "timeout":
            return "timeout"
        if err == "cancelled" or result.get("cancelled"):
            return "cancelled"
        return "failed"
    return "done"
