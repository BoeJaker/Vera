"""A delegated job as a TRAJECTORY - the unit of analysis and training (ROADMAP J7).

User, 2026-09-29: delegated tasks should be analysable and trainable; their logs
reusable as training data (especially for System 1 decision models such as Jev
and Laya, which answer typed questions - choice / score / yes-no - over a state,
and need domain data because they do poorly zero-shot), and remembered as
templates and memories. Each record carries the DELEGATOR's overarching task and
the BRIEF the delegated agent was given.

A trajectory is built once, when the job ends, from the job and its loop's event
log (vera:loop:events:<session>, a ring that does not last), and kept durably:
    why      parent_task, delegator
    what     title, brief, plan, suggested caps, the composed goal
    how      tier, intent, plan style, the catalogue offered (and the intent-core
             front), each step with its tool calls - cap, args, the model's
             stated reason, ok/error, a short result
    outcome  status, report, report metrics, and a RATING added later.

Rules (user, 2026-09-29): the delegating agent rates the report; only a rated
job becomes a memory. Pure: dicts in, dicts out.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = 1
VERDICTS = ("useful", "partly", "wrong")
MAX_PARENT_TASK = 4000
MAX_DELEGATOR = 200
MAX_CALLS = 300
MAX_ARGS = 600
MAX_THOUGHT = 400
MAX_PREVIEW = 400
MAX_SUMMARY = 600
MAX_NOTES = 2000
MAX_MEMORY_FINDINGS = 1500

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")


def _s(v: Any, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _as_list(v: Any) -> List[Any]:
    return list(v) if isinstance(v, (list, tuple)) else []


def _bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


def _args(v: Any) -> Any:
    """Tool args, kept whole when small, else as a truncated string."""
    import json
    try:
        text = json.dumps(v, default=str)
    except Exception:
        text = str(v)
    return v if len(text) <= MAX_ARGS else text[:MAX_ARGS]


def section(report: str, name: str) -> str:
    """The body of a markdown report's `## <name>` section (case-insensitive,
    matched on the heading's start), or ''."""
    out: List[str] = []
    inside = False
    for line in str(report or "").splitlines():
        h = _HEADING.match(line)
        if h:
            if inside:
                break
            inside = h.group(1).strip().lower().startswith(name.lower())
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def build(job: Dict[str, Any], events: Iterable[Dict[str, Any]],
          metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The trajectory of a finished (or interrupted) job from its loop's events."""
    j = job or {}
    loop: Dict[str, Any] = {"tier": "", "intent": "", "plan_style": "", "toolkit": [],
                            "intent_core_front": [], "plan": [], "gate": None, "done_reason": ""}
    steps: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    pending: Dict[tuple, List[Dict[str, Any]]] = {}
    n_calls = 0
    unknown_tools = 0

    def step(sid: Any) -> Dict[str, Any]:
        k = str(sid if sid is not None else "?")
        if k not in steps:
            steps[k] = {"id": k, "title": "", "goal": "", "caps": [], "ok": None,
                        "summary": "", "calls": []}
            order.append(k)
        return steps[k]

    for e in events or ():
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "")
        if t.endswith(".toolkit") and not loop["toolkit"]:
            loop["toolkit"] = [str(c) for c in _as_list(e.get("toolkit"))]
        elif t == "agent_loop_v6.tier":
            loop["tier"] = _s(e.get("tier"), 20)
        elif t == "agent_loop_v6.intent":
            loop["intent"] = _s(e.get("intent"), 20)
        elif t == "agent_loop_v6.plan_style":
            loop["plan_style"] = _s(e.get("style") or e.get("effective") or e.get("plan_style"), 40)
        elif t == "agent_loop_v6.intent_core":
            loop["intent_core_front"] = [str(c) for c in _as_list(e.get("front"))]
        elif t == "agent_loop_v6.plan":
            loop["plan"] = [{"title": _s(s.get("title"), 200),
                             "caps": [str(c) for c in _as_list(s.get("caps"))]}
                            for s in _as_list(e.get("steps")) if isinstance(s, dict)]
        elif t == "agent_loop_v6.gate":
            loop["gate"] = {k: e.get(k) for k in ("verdict", "ok", "passed", "reason") if k in e} or None
        elif t.endswith(".done") and t.startswith("agent_loop"):
            loop["done_reason"] = _s(e.get("reason"), 80)
        elif t.endswith(".step_start"):
            s = step(e.get("step_id"))
            s["title"] = _s(e.get("title"), 200)
            s["goal"] = _s(e.get("goal"), 600)
            s["caps"] = [str(c) for c in _as_list(e.get("caps"))]
        elif t.endswith(".step_done"):
            s = step(e.get("step_id"))
            s["ok"] = _bool(e.get("ok"))
            s["summary"] = _s(e.get("summary"), MAX_SUMMARY)
        elif t.endswith(".tool_call"):
            if n_calls >= MAX_CALLS:
                continue
            n_calls += 1
            c = {"cycle": e.get("cycle"), "tool": _s(e.get("tool"), 120),
                 "args": _args(e.get("args")), "thought": _s(e.get("thought"), MAX_THOUGHT),
                 "ok": None, "error": "", "result": "", "elapsed_ms": None}
            step(e.get("step_id"))["calls"].append(c)
            pending.setdefault((str(e.get("step_id")), str(e.get("cycle")), c["tool"]), []).append(c)
        elif t.endswith(".tool_done"):
            key = (str(e.get("step_id")), str(e.get("cycle")), _s(e.get("tool"), 120))
            q = pending.get(key) or []
            if q:
                c = q.pop(0)
            else:
                # A done with no call: the model named a tool that does not exist
                # (or a fast-path call). Kept - a wrong tool name is a lesson too.
                if n_calls >= MAX_CALLS:
                    continue
                n_calls += 1
                c = {"cycle": e.get("cycle"), "tool": key[2], "args": None, "thought": "",
                     "ok": None, "error": "", "result": "", "elapsed_ms": None}
                step(e.get("step_id"))["calls"].append(c)
            c["ok"] = _bool(e.get("ok"))
            c["error"] = _s(e.get("error"), MAX_PREVIEW)
            c["result"] = _s(e.get("preview"), MAX_PREVIEW)
            c["elapsed_ms"] = e.get("elapsed_ms")
            if c["ok"] is False and "no capability" in c["error"].lower():
                unknown_tools += 1

    report = str(j.get("report") or "")
    calls = [c for k in order for c in steps[k]["calls"]]
    return {
        "schema": SCHEMA,
        "job_id": _s(j.get("id"), 40),
        "session_id": _s(j.get("session_id"), 80),
        "title": _s(j.get("title"), 200),
        "parent_task": _s(j.get("parent_task"), MAX_PARENT_TASK),
        "delegator": _s(j.get("delegator"), MAX_DELEGATOR),
        "brief": str(j.get("brief") or ""),
        "plan": [str(x) for x in _as_list(j.get("plan"))],
        "suggest_caps": [str(x) for x in _as_list(j.get("suggest_caps"))],
        "goal": str(j.get("goal") or ""),
        "mode": _s(j.get("mode"), 20), "effort": _s(j.get("effort"), 20),
        "ref": _s(j.get("ref"), 120), "head": _s(j.get("head"), 40),
        "plan_style_requested": _s(j.get("plan_style"), 40),
        "created_at": _s(j.get("created_at"), 40), "ended_at": _s(j.get("ended_at"), 40),
        "status": _s(j.get("status"), 20), "error": _s(j.get("error"), 500),
        "loop": loop,
        "steps": [steps[k] for k in order],
        "counts": {"steps": len(order), "calls": len(calls),
                   "failed_calls": sum(1 for c in calls if c["ok"] is False),
                   "unknown_tools": unknown_tools,
                   "truncated": n_calls >= MAX_CALLS},
        "report": report,
        "metrics": dict(metrics or {}),
        "rating": None,
        "memory_ids": [],
    }


def rate(traj: Dict[str, Any], verdict: str, notes: str = "", by: str = "",
         at: str = "") -> Dict[str, Any]:
    """The trajectory with its rating set (a later rating replaces an earlier
    one; the earlier is kept in `rating_history`). Raises ValueError on an
    unknown verdict."""
    v = str(verdict or "").strip().lower()
    if v not in VERDICTS:
        raise ValueError("verdict must be one of %s" % ", ".join(VERDICTS))
    out = dict(traj or {})
    hist = list(out.get("rating_history") or [])
    if out.get("rating"):
        hist.append(out["rating"])
    out["rating_history"] = hist
    out["rating"] = {"verdict": v, "notes": _s(notes, MAX_NOTES), "by": _s(by, 120), "at": _s(at, 40)}
    return out


def memory_text(traj: Dict[str, Any]) -> str:
    """What a RATED job leaves in long-term memory: the overarching task, what was
    asked, what came back and how good it was - so a later planner or delegator
    recalls it. '' for an unrated job (only rated jobs are remembered)."""
    t = traj or {}
    r = t.get("rating") or {}
    if not r.get("verdict"):
        return ""
    report = str(t.get("report") or "")
    summary = section(report, "summary") or report[:MAX_SUMMARY]
    findings = section(report, "finding")[:MAX_MEMORY_FINDINGS]
    lines = ["Delegated task %s (%s) - rated %s by %s."
             % (t.get("job_id", ""), t.get("title", ""), r.get("verdict"), r.get("by") or "?")]
    if t.get("parent_task"):
        lines.append("Part of: %s" % str(t["parent_task"])[:600])
    lines.append("Asked: %s" % str(t.get("brief") or "")[:800])
    lines.append("Checkout: %s @ %s; %d steps, %d tool calls; status %s."
                 % (t.get("ref", ""), t.get("head", ""), (t.get("counts") or {}).get("steps", 0),
                    (t.get("counts") or {}).get("calls", 0), t.get("status", "")))
    if r.get("verdict") == "wrong":
        lines.append("The report was judged WRONG - do not rely on it. Why: %s" % (r.get("notes") or "-"))
    else:
        lines.append("Found: %s" % summary[:MAX_SUMMARY])
        if findings:
            lines.append("Findings:\n%s" % findings)
        if r.get("notes"):
            lines.append("Rating notes: %s" % r["notes"])
    return "\n".join(lines)


def memory_tags(traj: Dict[str, Any]) -> str:
    t = traj or {}
    r = t.get("rating") or {}
    tags = ["delegate", "verdict:%s" % (r.get("verdict") or "unrated")]
    if (t.get("loop") or {}).get("intent"):
        tags.append("intent:%s" % t["loop"]["intent"])
    if t.get("mode"):
        tags.append("mode:%s" % t["mode"])
    return ",".join(tags)


def memory_importance(traj: Dict[str, Any]) -> float:
    return {"useful": 0.7, "partly": 0.5, "wrong": 0.4}.get(
        ((traj or {}).get("rating") or {}).get("verdict") or "", 0.3)


def summary_row(traj: Dict[str, Any]) -> Dict[str, Any]:
    """The listing line for a trajectory."""
    t = traj or {}
    return {"job_id": t.get("job_id"), "title": t.get("title"), "status": t.get("status"),
            "parent_task": str(t.get("parent_task") or "")[:160],
            "delegator": t.get("delegator"), "ended_at": t.get("ended_at"),
            "steps": (t.get("counts") or {}).get("steps"), "calls": (t.get("counts") or {}).get("calls"),
            "verdict": ((t.get("rating") or {}).get("verdict")), "memory_ids": t.get("memory_ids") or []}
