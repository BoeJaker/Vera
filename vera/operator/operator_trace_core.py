"""Reduce one operator run's events to a diagnostic digest.

`operator.run` drives a real browser through an observe → think → act loop, and
until now it was the only major subsystem in Vera you could not read back. It
emits 15 event types where the agentic loop emits 288, and none of them were
persisted — so a finished run could not be re-examined at all, and a running one
could only be watched live or not at all.

That is not merely untidy. Across census runs 12 and 13, **every goal with an
`operator.run` step consumed its entire 25-minute wall cap** while every goal
without one finished in under seven minutes — and there was no trace to say what
the browser did with that time. `build-browser-verified` spent 15 cycles on step
1 alone. The one subsystem reliably eating the budget was the one subsystem with
no instrumentation.

This module is the operator's answer to `loop_trace_core`: the pure fold from an
event list to "what was it trying to do, what did it actually do, and did any of
it work". The capability layer does the Redis reads; everything here is a fold,
so the interesting judgements are testable without a browser.

## What it refuses to do

It reports no verdict on whether the run achieved its goal. The operator's own
`reason` is recorded as what the run CLAIMED, alongside the evidence — L8 exists
precisely because an operator run could report success after hitting its step
ceiling, so `reason: "done"` is a claim to check, not a result to trust.
`ceiling_hit` is surfaced next to it for exactly that reason.

Pure: no Redis, no app imports.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# The observe→think→act phases the operator loop moves through. Kept here so a
# digest can report phase coverage without importing the loop itself.
PHASES = ("observe", "think", "act", "verify")

# A run that stops because it ran out of steps has NOT necessarily finished its
# goal — the distinction L8 was landed for.
CEILING_REASONS = ("max_steps", "step_ceiling", "ceiling", "budget")


def _clip(v: Any, n: int = 300) -> str:
    s = str(v or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def digest_events(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold one operator run's events into a readable digest.

    Mirrors `loop_trace_core.digest_events` deliberately: same shape of answer
    (what was planned/attempted, what each step did, counters, warnings) so the
    two subsystems can be read the same way and eventually rendered by the same
    surface.
    """
    run: Dict[str, Any] = {}
    steps: List[Dict[str, Any]] = []
    warnings: List[str] = []
    started = ended = None

    for e in events or []:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "")
        ts = e.get("ts")
        if t.endswith("operator.run"):
            stage = str(e.get("stage") or "")
            if stage == "start":
                started = ts
                run["run_id"] = e.get("run_id")
                run["goal"] = _clip(e.get("goal"))
                run["target"] = e.get("target")
            elif stage == "done":
                ended = ts
                run["reason"] = e.get("reason")
                run["steps_reported"] = e.get("steps")
                run["gif"] = e.get("gif") or ""
        elif t.endswith("operator.step"):
            steps.append({
                "i": e.get("i"),
                "phase": e.get("phase") or "",
                "action": _clip(e.get("action"), 200),
                "thought": _clip(e.get("thought"), 300),
                "reason": _clip(e.get("reason"), 200),
                "error": _clip(e.get("error"), 200),
                "screenshot": e.get("screenshot") or "",
                "ts": ts,
            })
        elif t.endswith("operator.session"):
            run.setdefault("session_stage", e.get("stage"))

    # ── counters ────────────────────────────────────────────────────────────
    phases: Dict[str, int] = {}
    for s in steps:
        p = str(s.get("phase") or "?")
        phases[p] = phases.get(p, 0) + 1
    errors = [s for s in steps if s.get("error")]
    with_shot = [s for s in steps if s.get("screenshot")]

    # Repeated identical actions are the operator's thrash signature — the
    # browser equivalent of a step burning cycles without moving.
    seen: Dict[str, int] = {}
    for s in steps:
        a = s.get("action") or ""
        if a:
            seen[a] = seen.get(a, 0) + 1
    repeats = {a: n for a, n in seen.items() if n >= 3}

    reason = str(run.get("reason") or "")
    ceiling_hit = any(k in reason.lower() for k in CEILING_REASONS)

    for a, n in sorted(repeats.items(), key=lambda kv: -kv[1]):
        warnings.append(f"the same action was attempted {n}x: {a}")
    for s in errors:
        warnings.append(f"step {s.get('i')} ({s.get('phase')}) errored: {s.get('error')}")
    if ceiling_hit:
        warnings.append(
            f"this run stopped because it ran out of steps (reason={reason!r}) — "
            f"that is NOT evidence the goal was reached; check the last screenshot")
    if steps and not with_shot:
        warnings.append("no step captured a screenshot — nothing visual to verify against")
    if not steps:
        warnings.append("no steps recorded — the run never got as far as acting")

    counters = {
        "events": len(events or []),
        "steps": len(steps),
        "errors": len(errors),
        "screenshots": len(with_shot),
        "phases": phases,
        "repeated_actions": len(repeats),
        # The operator's own claim vs the ceiling, side by side, because L8.
        "ceiling_hit": ceiling_hit,
    }
    return {"run": run, "steps": steps, "counters": counters, "warnings": warnings,
            "started_at": started, "ended_at": ended,
            "duration_s": _duration(started, ended)}


def _duration(a: Any, b: Any) -> Optional[float]:
    """Seconds between two ISO timestamps, or None if either is unusable."""
    from datetime import datetime
    try:
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        db = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return round((db - da).total_seconds(), 1)
    except Exception:
        return None


def summarise_run(run_id: str, events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """One line per run, for a list view."""
    d = digest_events(events)
    r, c = d["run"], d["counters"]
    return {
        "run_id": r.get("run_id") or run_id,
        "goal": r.get("goal") or "",
        "target": r.get("target"),
        "reason": r.get("reason"),
        "steps": c["steps"],
        "errors": c["errors"],
        "repeated_actions": c["repeated_actions"],
        "ceiling_hit": c["ceiling_hit"],
        "duration_s": d["duration_s"],
        "started_at": d["started_at"],
        # A run with no `done` event never finished — it was cancelled, or the
        # process died holding it (observed: an operator.think generation ran
        # 1211s against the shared GPU after its census run was cancelled).
        "completed": bool(d["ended_at"]),
    }
