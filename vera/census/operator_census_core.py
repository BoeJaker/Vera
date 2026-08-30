"""The operator census: measuring browser runs over time.

The loop census exists because the agentic loop needed a measuring instrument.
`operator.run` needs one for the same reason and does not have one — and the
evidence that it needs one is strong: across loop-census runs 12 and 13, EVERY
goal with an `operator.run` step consumed its entire 25-minute wall cap, while
every goal without one finished in under seven minutes. The browser was the most
expensive part of the system and the least measured.

This reuses the loop census's file/ordering machinery (`census_core`) and its
per-run digest (`operator_trace_core`), but it deliberately does NOT reuse the
loop's scoring, because the two subsystems fail differently:

  * a loop goal's coarse outcome is `done` vs `wall-cap`;
  * a browser run's is whether it REACHED the goal, and the interesting failure
    is a run that says it did while having merely run out of steps.

## The outcome ladder, worst to best

    incomplete  no `done` event at all — cancelled, or its process died holding
                it. Worst because nothing about it can be trusted, including its
                duration.
    ceiling     it stopped because it ran out of STEPS. Its own `reason` may
                still read like success — L8 was landed precisely because an
                operator run could report success after hitting its ceiling — so
                this ranks below a run that errored honestly.
    errored     it finished, but steps failed along the way.
    finished    it finished with no step errors and no ceiling.

A run that is BOTH at its ceiling and errored takes the lower rank: the ceiling
makes its self-report unreliable, which is the more serious problem.

Note what is absent: there is no `success` rank. Nothing here decides whether the
browser actually achieved the goal — only a human or a checked assertion can say
that, and `finished` means "ran cleanly to a stop", not "did the thing".

Pure: no Redis, no app imports.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

INCOMPLETE, CEILING, ERRORED, FINISHED = "incomplete", "ceiling", "errored", "finished"

# Worst → best. Used only to give a CHANGE a direction; it is not a score.
_RANK = {INCOMPLETE: 0, CEILING: 1, ERRORED: 2, FINISHED: 3}


def outcome_of(rec: Dict[str, Any]) -> str:
    """Classify one recorded operator run.

    Order matters: incomplete beats everything, then ceiling, then errors. A run
    at its ceiling AND with errors is reported as `ceiling`, because an
    unreliable self-report is worse than a visible failure.
    """
    if not isinstance(rec, dict):
        return INCOMPLETE
    if not rec.get("completed"):
        return INCOMPLETE
    if rec.get("ceiling_hit"):
        return CEILING
    if int(rec.get("errors") or 0) > 0:
        return ERRORED
    return FINISHED


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarise_run(run_id: str, records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """One operator-census run's headline numbers."""
    recs = [r for r in (records or []) if isinstance(r, dict)]
    by: Dict[str, int] = {}
    for r in recs:
        o = outcome_of(r)
        by[o] = by.get(o, 0) + 1
    durs = [d for d in (_num(r.get("duration_s")) for r in recs) if d is not None]
    return {
        "run_id": run_id,
        "goals": len(recs),
        "finished": by.get(FINISHED, 0),
        "errored": by.get(ERRORED, 0),
        "ceiling": by.get(CEILING, 0),
        "incomplete": by.get(INCOMPLETE, 0),
        "steps_total": sum(int(r.get("steps") or 0) for r in recs),
        "errors_total": sum(int(r.get("errors") or 0) for r in recs),
        # The browser thrash signature, aggregated: how many goals had ANY action
        # attempted three or more times.
        "thrashing_goals": sum(1 for r in recs if int(r.get("repeated_actions") or 0) > 0),
        "duration_total_s": round(sum(durs), 1),
        # A run where anything is incomplete cannot be compared cleanly: an
        # incomplete goal has no trustworthy duration or step count.
        "comparable": bool(recs) and by.get(INCOMPLETE, 0) == 0,
    }


def compare_runs(base: Sequence[Dict[str, Any]],
                 head: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-goal outcome transition, worst news first.

    Per goal, never aggregated — the same reason as the loop census: browser
    goals differ enormously in cost, so a run-level mean says nothing.
    `outcome_change` is a transition to investigate, not a score; `finished`
    means "ran cleanly to a stop", not "achieved the goal".
    """
    b = {r.get("id"): r for r in (base or []) if isinstance(r, dict)}
    h = {r.get("id"): r for r in (head or []) if isinstance(r, dict)}
    out: List[Dict[str, Any]] = []
    for gid in sorted(set(b) | set(h), key=lambda x: str(x)):
        br, hr = b.get(gid), h.get(gid)
        if br is None or hr is None:
            out.append({"id": gid, "outcome_change": "missing",
                        "base_outcome": outcome_of(br) if br else None,
                        "head_outcome": outcome_of(hr) if hr else None,
                        "note": "not present in both runs — not comparable"})
            continue
        bo, ho = outcome_of(br), outcome_of(hr)
        d = _RANK[ho] - _RANK[bo]
        rec: Dict[str, Any] = {
            "id": gid,
            "outcome_change": "improved" if d > 0 else ("regressed" if d < 0 else "held"),
            "base_outcome": bo, "head_outcome": ho,
            "base_steps": br.get("steps"), "head_steps": hr.get("steps"),
            "base_errors": br.get("errors"), "head_errors": hr.get("errors"),
            "head_repeated_actions": hr.get("repeated_actions"),
            "base_duration_s": _num(br.get("duration_s")),
            "head_duration_s": _num(hr.get("duration_s")),
        }
        if ho == CEILING:
            rec["note"] = ("stopped at its step ceiling — its own reason may still "
                           "read like success, so it settles nothing")
        elif ho == INCOMPLETE:
            rec["note"] = ("no completion event — cancelled or its process died "
                           "holding it; its duration and step count mean nothing")
        elif hr.get("repeated_actions"):
            rec["note"] = (f"{hr.get('repeated_actions')} action(s) attempted 3+ times "
                           f"— the browser thrash signature")
        out.append(rec)
    order = {"regressed": 0, "missing": 1, "held": 2, "improved": 3}
    out.sort(key=lambda r: (order.get(r["outcome_change"], 4), str(r["id"])))
    return out


def record_from_trace(goal_id: str, trace: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one `operator.trace` digest into a census record.

    Keeps the run_id so a census row can always be opened back up into the full
    step-by-step trace — the thing the loop census could not do for its browser
    goals, and the whole reason O13 came first.
    """
    run = (trace or {}).get("run") or {}
    c = (trace or {}).get("counters") or {}
    return {
        "id": goal_id,
        "run_id": run.get("run_id") or (trace or {}).get("run_id") or "",
        "goal": run.get("goal") or "",
        "target": run.get("target"),
        "reason": run.get("reason"),
        "steps": int(c.get("steps") or 0),
        "errors": int(c.get("errors") or 0),
        "screenshots": int(c.get("screenshots") or 0),
        "repeated_actions": int(c.get("repeated_actions") or 0),
        "ceiling_hit": bool(c.get("ceiling_hit")),
        "duration_s": (trace or {}).get("duration_s"),
        "completed": bool((trace or {}).get("ended_at")),
        "warnings": (trace or {}).get("warnings") or [],
    }
