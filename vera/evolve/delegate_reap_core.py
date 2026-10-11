"""Which delegated jobs a prod restart left behind (roadmap J5c, compute-roles 11 Oct 2026).

A delegated job runs as an asyncio task in prod. A restart kills the task, but the job record stays
"running", its detached worktree stays registered and its loop's run hash says "running" forever -
reads only ever *reported* it as interrupted. Once at start, the host settles such jobs: status
interrupted, its own worktree removed (exact path, never prune), the run hash closed, the trajectory
kept. A job counts as left behind only when no task runs it in this process AND its loop has been
silent for STALE_S - so a job another live process is running is never touched.

Pure: job + last loop event time in, a verdict out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

STALE_S = 600
ACTIVE = ("starting", "running", "queued")


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        s = str(iso).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def should_reap(job: Dict[str, Any], *, running_here: bool, last_event_iso: Optional[str],
                now: float, stale_s: int = STALE_S) -> Tuple[bool, str]:
    """(True, why) when this job was left behind by a restart."""
    if not isinstance(job, dict) or str(job.get("status") or "") not in ACTIVE:
        return False, "not active"
    if running_here:
        return False, "a task runs it in this process"
    last = _epoch(last_event_iso) or _epoch(job.get("updated_at")) or _epoch(job.get("created_at"))
    if last is None:
        return True, "no loop event and no timestamps"
    idle = now - last
    if idle < stale_s:
        return False, "its loop was active %d s ago (another process may run it)" % int(idle)
    return True, "no task here and its loop silent for %d s" % int(idle)
