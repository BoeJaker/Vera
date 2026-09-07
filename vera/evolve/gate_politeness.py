"""Is the box free? — the discipline the census harness learned and the suite lacks.

Step 2 of flattening the census into the Loop Lab suite. The suite runner
already bounds a task (`timeout_s`), kills an idle run (`run_idle_timeout_s`)
and cancels cleanly. What it has never had is POLITENESS: it starts the next
task regardless of who else is using the GPU.

That matters because the ollama GPU gate is **capacity 1**. A suite started
while another agent, a dream cycle or a chat is mid-generation does not run in
parallel with it - it QUEUES behind it, and the waiting is charged to the task's
own `timeout_s`. The task then reads as slow or timed out when nothing was
wrong with it. The census harness has waited for the box since its early runs
for exactly this reason, and it is the one piece of its behaviour that cannot be
dropped in the migration.

Ported verbatim in meaning from `run_census.py::box_is_busy` / `wait_for_free`:

    a gated node with held > 0   -> "gate held by <owners>"
    any loop session running     -> "another loop is running"
    otherwise                    -> free

Deliberately NOT a lock. It is a courtesy check with a ceiling: if the box never
frees, the suite proceeds anyway and says so, because a benchmark that silently
never runs is worse than one that runs contended and is labelled as such.

Pure: the caller supplies the readings.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: How long to wait for the box before giving up and running anyway. An hour
#: matches the harness; a long agent loop can legitimately hold the GPU for
#: tens of minutes.
DEFAULT_MAX_WAIT_S = 3600

#: Gap between checks. Long enough not to spam the gate, short enough that a
#: freed box is picked up promptly.
POLL_SECONDS = 30

#: Task types that actually contend for the GPU. Cap smoke-tests are plumbing
#: checks measured in seconds - making them queue behind a 20-minute loop would
#: destroy the "counter moves immediately" property the suite deliberately has.
GPU_BOUND_TYPES = ("loop", "sim")


def needs_free_box(task_type: str) -> bool:
    """True when this task should wait its turn."""
    return str(task_type or "loop").strip().lower() in GPU_BOUND_TYPES


def busy_reason(gate: Optional[Dict[str, Any]], running_loops: int = 0) -> str:
    """Why the box is busy, or "" when it is free.

    ``gate`` is an ``ollama.gate.status`` payload. A node only counts when it is
    GATED - an ungated node has no capacity limit to contend for - and only when
    it is actually held.
    """
    for node in ((gate or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        try:
            held = int(node.get("held") or 0)
        except (TypeError, ValueError):
            held = 0
        if node.get("gated") and held > 0:
            owners = node.get("owners") or "?"
            return "gate held by %s" % (owners,)
    try:
        if int(running_loops or 0) > 0:
            return "another loop is running"
    except (TypeError, ValueError):
        pass
    return ""


def describe_wait(reason: str, waited_s: float, max_wait_s: int = DEFAULT_MAX_WAIT_S) -> str:
    """One line for the suite's progress feed, so a wait is visible rather than
    looking like a hang."""
    return ("waiting for the box: %s (%ds of %ds)"
            % (reason or "busy", int(waited_s), int(max_wait_s)))


def gave_up(waited_s: float, max_wait_s: int = DEFAULT_MAX_WAIT_S) -> str:
    """What to record when the ceiling is reached. The suite runs anyway."""
    return ("the box stayed busy for %ds - running anyway, so this task's timing "
            "is contended and should not be compared with an uncontended run"
            % int(waited_s))
