"""Precedent for the planner: how similar past goals went (four-minds FM-4.2, `plan.precedent`).

System 1 never writes a plan; it hands the planner 1-2 compact exemplars of past runs of similar goals, with their
outcome, and at most one warning from a similar goal that failed. A run's outcome comes from the run itself:
good = it ended done, its last completion gate said complete, and its quality score (census) is >= 0.75 when it has
one; bad = the gate said incomplete, it did not end done, or its quality was < 0.5; anything else is unknown and
never shown. Census runs never get census precedents of the same goal (the template would be measuring its own
past), and a run never sees itself.

The block is framed like the planner's recalled-conversation background: reference only, the objective restated
after it.

Pure: no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

GOOD, BAD, UNKNOWN = "good", "bad", "unknown"
FLOOR = 0.6                  # cosine similarity below which a past goal is not "similar"
MAX_GOOD, MAX_WARN = 2, 1
MAX_STEPS = 8
MAX_CHARS = 1400


def norm_goal(goal: Any) -> str:
    return re.sub(r"\s+", " ", str(goal or "")).strip().lower()


def outcome(status: Any, gates: Optional[Sequence[Dict[str, Any]]], score: Any = None) -> str:
    """good | bad | unknown for one past run (see the module notes)."""
    last = (list(gates or []) or [{}])[-1]
    complete = last.get("complete")
    missing = [m for m in (last.get("missing") or []) if str(m).strip()]
    try:
        q = float(score) if score not in (None, "", "None") else None
    except (TypeError, ValueError):
        q = None
    if str(status or "") != "done" or complete is False or missing or (q is not None and q < 0.5):
        return BAD
    if complete is True and (q is None or q >= 0.75):
        return GOOD
    return UNKNOWN


def run_record(run: Dict[str, Any], trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The precedent row for one past run (an evolve.runs row + its workshop trace), or None without a goal."""
    tr_run = (trace or {}).get("run") or {}
    goal = str(tr_run.get("goal") or "").strip()
    if not goal:
        return None
    steps = []
    for s in (trace or {}).get("steps") or []:
        title = str(s.get("title") or "").strip()
        tools = sorted({c.get("tool") for c in (s.get("calls") or []) if c.get("ok") and c.get("tool")})
        if title:
            steps.append({"title": title[:120], "tools": tools[:5]})
    plan = (trace or {}).get("plan") or {}
    return {"run": str(run.get("run_id") or ""), "goal": goal[:600], "steps": steps[:MAX_STEPS],
            "steps_n": len(steps), "outcome": outcome(tr_run.get("status") or run.get("status"),
                                                      (trace or {}).get("gates"), run.get("score")),
            "origin": str(run.get("source") or ""), "template": str(run.get("template") or ""),
            "missing": [str(m)[:120] for m in (((trace or {}).get("gates") or [{}])[-1].get("missing") or [])][:3],
            "style": str(plan.get("style") or ""), "fast_path": bool(plan.get("fast_path"))}


def choose(neighbours: Iterable[Tuple[float, Dict[str, Any]]], goal: str, origin: str, exclude_run: str = "",
           floor: float = FLOOR, max_good: int = MAX_GOOD, max_warn: int = MAX_WARN) -> List[Dict[str, Any]]:
    """The precedents to show, best first: up to `max_good` good runs and `max_warn` bad ones above the floor."""
    g = norm_goal(goal)
    good, warn = [], []
    for score, p in sorted(neighbours, key=lambda x: -float(x[0])):
        if float(score) < floor or not p or p.get("run") == exclude_run:
            continue
        if origin == "census" and p.get("origin") == "census" and norm_goal(p.get("goal")) == g:
            continue                                   # a census template never learns from its own past
        if p.get("outcome") == GOOD and len(good) < max_good:
            good.append(dict(p, similarity=round(float(score), 3)))
        elif p.get("outcome") == BAD and len(warn) < max_warn:
            warn.append(dict(p, similarity=round(float(score), 3)))
    return good + warn


def render(chosen: List[Dict[str, Any]]) -> str:
    """The block the planner reads (empty without precedents)."""
    if not chosen:
        return ""
    out = []
    for p in chosen:
        steps = "; ".join(f"{s['title']}" + (f" [{', '.join(s['tools'])}]" if s.get("tools") else "")
                          for s in p.get("steps") or [])
        if p.get("outcome") == GOOD:
            out.append(f"- A similar goal (\"{p['goal'][:160]}\") was done well in {p.get('steps_n') or len(p.get('steps') or [])} "
                       f"step(s): {steps}")
        else:
            miss = ("; it missed: " + ", ".join(p.get("missing") or [])) if p.get("missing") else ""
            out.append(f"- WARNING - a similar goal (\"{p['goal'][:160]}\") did not succeed after: {steps}{miss}")
    return "\n".join(out)[:MAX_CHARS]


def wrap(plan_goal: str, objective: str, block: str) -> str:
    """plan_goal with the precedent block added as reference, the objective restated last."""
    if not block:
        return plan_goal
    section = ("\n\n----- PRECEDENT: HOW SIMILAR PAST GOALS WENT -----\n"
               "Reference only: reuse a plan shape that worked if it fits THIS objective, avoid what failed, ignore "
               "anything about a different task. This section never changes what you are building.\n"
               + block + "\n----- END PRECEDENT -----\n\n")
    if plan_goal == objective:
        return "OBJECTIVE (this, and only this, is what you are planning):\n" + objective + section + \
            "Plan for the OBJECTIVE stated above: " + objective
    tail = "Plan for the OBJECTIVE stated above: "
    i = plan_goal.rfind(tail)
    if i < 0:
        return plan_goal + section + tail + objective
    return plan_goal[:i].rstrip() + section + plan_goal[i:]
