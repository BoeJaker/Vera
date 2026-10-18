"""Which adopted pipelines had their gate cut off by a restart, and what they had passed. Pure.

The critical-tier gate runs INSIDE evolve.pipeline.adopt's call; the pipeline record in
Redis is all that outlives the process. 4 Oct 2026 a release restarted prod while two gates
sat "critical tier queued behind another gate in this process": the process they waited for
was gone, nothing would ever run them, and the Redis lease of the gate that died kept the
slot for up to 20 min. evolve's startup resumes them with these helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

#: A gate older than this is not resumed - re-adopt it.
MAX_AGE_H = 24
#: What the record says while its gate is waiting or running.
IN_FLIGHT_PREFIXES = ("critical tier queued", "running critical tier", "gate resumed after restart")


def _parse(ts: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def interrupted(rec: Dict[str, Any], now: datetime, max_age_h: float = MAX_AGE_H) -> bool:
    """An adopted pipeline whose critical-tier gate was queued or running and never
    finished - and is recent enough to resume."""
    if not isinstance(rec, dict) or not rec.get("adopted"):
        return False
    if rec.get("gate_passed") is not None or rec.get("decision") not in (None, "", "pending"):
        return False
    if str(rec.get("status") or "adopted") != "adopted" or not rec.get("branch"):
        return False
    cur = str(rec.get("current") or "")
    steps = rec.get("steps") or []
    last = str((steps[-1] or {}).get("stage") or "") if steps else ""
    # "test-pool": a POOLED gate's notes ("N test files in 12 chunks ...", "test runners all busy ...") are its last
    # step while its chunks wait or run - before 18 Oct 2026 a restart then left the pipeline adopted with no gate,
    # forever (feat/models-performance-view sat 9 h; the "stranded pipelines" vera-d0 reported)
    if not (cur.startswith(IN_FLIGHT_PREFIXES) or last in ("critical-queue", "gate-resume", "test-pool")):
        return False
    made = _parse(rec.get("created_at") or "")
    return made is not None and now - made <= timedelta(hours=max_age_h)


def compile_ok(steps: Iterable[Dict[str, Any]]) -> bool:
    """The compile and panel checks the adopt already recorded (they ran before the tier)."""
    return all(bool(s.get("ok")) for s in (steps or []) if s.get("stage") in ("gate", "gate-ui"))


def orphaned_lease(holder: Any, ids: Iterable[str]) -> bool:
    """The gate lease token is '<pipeline id>:<branch>': held by a pipeline that died."""
    h = holder.decode("utf-8", "replace") if isinstance(holder, bytes) else str(holder or "")
    return bool(h) and h.split(":", 1)[0] in set(ids)


def resume_order(recs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Oldest first - the order they were queued in."""
    return sorted(list(recs), key=lambda r: str(r.get("created_at") or ""))


def latest_per_branch(recs: Iterable[Dict[str, Any]]):
    """(keep, superseded): the newest pipeline per branch, oldest first; and each older one
    with the id of the pipeline that supersedes it. 4 Oct 2026: the first resume picked up
    18 gates, several the same branch re-adopted two or three times."""
    newest: Dict[str, Dict[str, Any]] = {}
    for r in recs or []:
        b = str(r.get("branch") or "")
        if b not in newest or str(r.get("created_at") or "") > str(newest[b].get("created_at") or ""):
            newest[b] = r
    keep = resume_order(newest.values())
    superseded = [(r, newest[str(r.get("branch") or "")].get("id"))
                  for r in recs or [] if newest.get(str(r.get("branch") or "")) is not r]
    return keep, superseded


def newer_pipeline(rec: Dict[str, Any], rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newest OTHER pipeline for the same branch, created after `rec`, in any state - or
    None. An orphan with one is not worth re-running: the branch was re-adopted (and has
    passed, failed or is gating) after the restart cut this one off. 4 Oct 2026: orphans of
    branches already re-gated and live were re-run by the resume."""
    b, made, rid = str(rec.get("branch") or ""), str(rec.get("created_at") or ""), rec.get("id")
    best: Optional[Dict[str, Any]] = None
    for r in rows or []:
        if not isinstance(r, dict) or r.get("id") == rid or str(r.get("branch") or "") != b:
            continue
        if str(r.get("created_at") or "") > made and (
                best is None or str(r.get("created_at") or "") > str(best.get("created_at") or "")):
            best = r
    return best
