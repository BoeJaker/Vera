"""One durable record per loop run - the shape all three loop histories share.

Before this there were three stores with three different retentions, and a run
appeared in a surface only by accident of which one happened to keep it:

  * chat's Loops pane   `vera:loop:run:<sid>` + `vera:loop:sessions`
                        - the run hash carried _RESUME_TTL (7 DAYS) because that
                          window was sized for RESUMING a run, not for history,
                          so the pane silently aged out at a week; and the zset
                          index had no TTL and no trim, so it grew forever as
                          tombstones pointing at expired hashes (the endpoint
                          skips them with `if not run: continue`, which is why
                          the pane showed 9 one hour and 22 the next).
  * project pages       `vera:dream:project_loops:<slug>` - durable, but written
                          ONLY when a run carries a project slug.
  * `agent_loop.trace`  read the live event stream, same 7-day window.

This module owns the canonical summary: small (a few hundred bytes), no TTL, and
retained by an explicit policy rather than by a resume window. The heavy replay
event list keeps _RESUME_TTL - resuming really is a short-lived concern.

**Unprojected runs are NOT filed into the project store.** That was considered
and rejected: the project store means "belongs to this project", and filling it
with scratch runs would destroy that meaning. They live here instead, and the
project store keeps referencing this record for the runs it does own.

Pure: no Redis, no app imports. The callers do I/O; this decides SHAPE and
RETENTION, which is the part worth testing.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The user's policy, 2026-08-27: keep the last 2,000 runs AND 90 days, both
# settable from the UI. Two bounds, not one: a count alone lets a quiet month
# vanish, and an age alone lets a busy day balloon the store.
DEFAULT_MAX_RUNS = 2000
DEFAULT_MAX_AGE_DAYS = 90

# Bounds for what the UI may set, so a fat-fingered value cannot wedge the store.
MIN_MAX_RUNS, MAX_MAX_RUNS = 10, 100_000
MIN_MAX_AGE_DAYS, MAX_MAX_AGE_DAYS = 1, 3650

# Fields kept per run. Deliberately a summary, not a transcript: this is what a
# history list and a census need. Full events stay in the replay list.
SUMMARY_FIELDS = (
    "session_id", "goal", "engine", "source", "status",
    "started_at", "updated_at", "project_slug",
    "planned_steps", "executed_steps", "inserted_steps",
    "tool_calls", "cycles_per_step", "warnings_count", "code_version",
)


def normalise_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """UI-supplied retention config -> a safe, clamped policy.

    Anything missing or unparseable falls back to the default rather than
    disabling retention: a bad value must never mean "keep nothing" (data loss)
    or "keep everything" (an unbounded store).
    """
    cfg = cfg or {}

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    return {
        "max_runs": _int("max_runs", DEFAULT_MAX_RUNS, MIN_MAX_RUNS, MAX_MAX_RUNS),
        "max_age_days": _int("max_age_days", DEFAULT_MAX_AGE_DAYS,
                             MIN_MAX_AGE_DAYS, MAX_MAX_AGE_DAYS),
    }


def summarise_run(run: Dict[str, Any], counters: Optional[Dict[str, Any]] = None,
                  *, warnings: Optional[Iterable[Any]] = None) -> Dict[str, str]:
    """Build the stored record. Values are strings - it lands in a Redis hash.

    Absent fields are OMITTED rather than written as "" or 0: a history row that
    claims `planned_steps=0` is worse than one that says nothing, because a
    census cannot tell "no steps" from "not recorded".
    """
    counters = counters or {}
    src: Dict[str, Any] = {
        "session_id":     run.get("session_id"),
        "goal":           (str(run.get("goal") or "")[:800] or None),
        "engine":         run.get("engine") or run.get("variant"),
        "source":         run.get("source"),
        "status":         run.get("status"),
        "started_at":     run.get("started_at"),
        "updated_at":     run.get("updated_at"),
        "project_slug":   run.get("project_slug") or run.get("slug"),
        "planned_steps":  counters.get("planned_steps"),
        "executed_steps": counters.get("executed_steps"),
        "inserted_steps": counters.get("inserted_steps"),
        "tool_calls":     counters.get("tool_calls"),
        "cycles_per_step": counters.get("cycles_per_step"),
        "code_version":   counters.get("code_version"),
        "warnings_count": (len(list(warnings)) if warnings is not None else None),
    }
    out: Dict[str, str] = {}
    for k in SUMMARY_FIELDS:
        v = src.get(k)
        if v is None or v == "":
            continue
        out[k] = v if isinstance(v, str) else str(v)
    return out


def ids_to_drop(scored: Iterable[Tuple[str, float]], cfg: Optional[Dict[str, Any]] = None,
                *, now: Optional[float] = None) -> List[str]:
    """Which ids fall outside the policy: too old OR beyond the count.

    `scored` is (session_id, unix_ts) newest-first or not - order is derived
    here rather than trusted, because the caller reads a zset that a concurrent
    writer may have reordered.
    """
    pol = normalise_config(cfg)
    now = time.time() if now is None else now
    cutoff = now - pol["max_age_days"] * 86400.0
    items = sorted(((str(i), float(s)) for i, s in scored), key=lambda x: -x[1])
    drop: List[str] = []
    for idx, (sid, ts) in enumerate(items):
        if idx >= pol["max_runs"] or ts < cutoff:
            drop.append(sid)
    return drop
