"""Every agent loop as a Loop Lab run record - not only the ones a census or a
Loop Lab task started.

Census goals reach the suite store through `evolve.result.ingest` and Loop Lab
tasks through `_run_task`; a loop started from the chat, the dream director, a
v8 program or the API left only its resume state (`vera:loop:run:<sid>`) and
the Loops pane's compact history, so Loop Lab's run list, drivers and activity
never saw most of the loops Vera actually runs.

This builds a run record from a finished loop's own events - the same trace
digest the census reads (`loop_trace_core.digest_events`) - in the shape every
`evolve.runs` reader already takes. The capability in task_history_capabilities
stores it.

Ownership rules, so one loop is one row:
  * run_id is the loop session. A census row is keyed by the same session
    (result_ingest_core.run_id_for), so the census's richer record replaces
    this one; this one never replaces a record another source wrote.
  * A Loop Lab task runs its loop under `evolve:<run_id>` and records itself,
    so that session is not recorded here.

Pure: no I/O.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional, Sequence, Tuple

SOURCE = "loop"
#: Sessions whose driver writes its own run record.
SELF_RECORDING_PREFIXES = ("evolve:",)
#: Session-id prefix -> the origin shown in Loop Lab. Anything else is a loop
#: started by a person (the chat) or a caller of the API; the id cannot tell
#: those apart, so they share one word.
ORIGIN_PREFIXES = (("dream:", "dream"), ("v8:", "program"), ("census", "census"),
                   ("delegate:", "delegate"))
ORIGIN_DEFAULT = "interactive"
FINAL_MAX = 12000


def is_self_recording(session_id: str) -> bool:
    return str(session_id or "").startswith(SELF_RECORDING_PREFIXES)


def origin_of(session_id: str) -> str:
    s = str(session_id or "")
    for prefix, origin in ORIGIN_PREFIXES:
        if s.startswith(prefix):
            return origin
    return ORIGIN_DEFAULT


def _ts(v: Any) -> Optional[datetime]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _engine(events: Sequence[Dict[str, Any]]) -> str:
    """The outermost loop engine: a v7 run emits v6's events too, and the
    record should name what was asked for."""
    best = ""
    for e in events:
        t = str(e.get("type") or "")
        head = t.split(".", 1)[0]
        if head.startswith("agent_loop") and head > best:
            best = head
    return best.replace("agent_loop_", "") or "loop"


def _terminal(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    for e in reversed(events):
        t = str(e.get("type") or "")
        if t.endswith(".done") or t.endswith(".error"):
            return e
    return {}


def _final_text(ev: Dict[str, Any]) -> str:
    for k in ("final", "result", "answer", "summary"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v[:FINAL_MAX]
    return ""


def delegate_job(run_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The delegated job's facts a loop's run hash carries (field `delegate`,
    JSON, written by evolve.delegate), or {}."""
    raw = (run_state or {}).get("delegate")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
    except Exception:
        return {}
    return v if isinstance(v, dict) else {}


def run_record_from_events(session_id: str, events: Sequence[Dict[str, Any]], digest: Dict[str, Any], *,
                           run_state: Optional[Dict[str, Any]] = None, where: str = "",
                           ingested_at: str = "") -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(compact, detail) for a finished loop, or None when the session is not
    one this should record (self-recording, or no terminal event yet)."""
    sid = str(session_id or "").strip()
    if not sid or is_self_recording(sid):
        return None
    term = _terminal(events)
    if not term:
        return None
    rs = run_state or {}
    ok = str(term.get("type") or "").endswith(".done") and not term.get("error")
    status = "done" if ok else "error"
    error = "" if ok else str(term.get("error") or "error")
    started = _ts(rs.get("started_at")) or next((d for d in (_ts(e.get("ts")) for e in events) if d), None)
    ended = _ts(term.get("ts")) or _ts(rs.get("updated_at"))
    elapsed = round((ended - started).total_seconds(), 1) if (started and ended) else 0.0
    counters = digest.get("counters") or {}
    plan = digest.get("plan") or {}
    origin = origin_of(sid)
    engine = _engine(events)
    goal = str(rs.get("goal") or "")
    # No checks: the suite's rule for a run with nothing to check is clean=1.0.
    pass_rate = 1.0 if ok else 0.0
    compact: Dict[str, Any] = {
        "run_id": sid,
        "task": "loop:%s" % origin,
        "label": (goal[:80] or sid),
        "task_type": "loop",
        "profile": "",
        "ts": str(term.get("ts") or rs.get("updated_at") or ""),
        "elapsed_s": elapsed,
        "pass_rate": pass_rate, "checks_ok": 0, "checks_n": 0,
        "score": None,
        "combined": round(pass_rate * 10, 1),
        "variant": "",
        "source": SOURCE,
        "session": "",
        "loop_session": sid,
        "error": error[:200],
        "where": str(where or ""),
        "triggered_by": origin,
        "status": status,
        "ok": ok,
        "origin": origin,
        "engine": engine,
        "tier": plan.get("tier"),
        "intent": plan.get("intent"),
        "plan_style": plan.get("style"),
        "plan_style_requested": plan.get("style_requested"),
        "fast_path": bool(plan.get("fast_path")),
        "entity_coverage": (plan.get("entity_coverage") or {}).get("ratio"),
        "intent_zeroshot": (plan.get("intent_zeroshot") or {}).get("zeroshot"),
        "intent_zeroshot_agrees": (plan.get("intent_zeroshot") or {}).get("agrees_used"),
        "planned": counters.get("planned_steps"),
        "executed": counters.get("executed_steps"),
        "inserted": counters.get("inserted_steps"),
        "tool_calls": counters.get("tool_calls"),
        "warnings": len(digest.get("warnings") or []),
        "code_version": list(counters.get("code_version") or [])[:3],
        "started_at": started.isoformat() if started else "",
        "ingested_at": str(ingested_at or ""),
    }
    job = delegate_job(rs)
    if job:
        # A delegated job (evolve.delegate): its own facts ride on its loop's record,
        # and its title - not the composed handover goal - labels the row.
        compact["delegate"] = job
        compact["label"] = (str(job.get("title") or "")[:80] or compact["label"])
    detail = dict(compact)
    detail.update({
        "goal": goal,
        "final": _final_text(term),
        "steps": digest.get("steps") or [],
        "checks": [],
        "raw_keys": [],
        "assessment": None,
        "blocked": False,
        "plan": plan,
        "control": digest.get("control") or [],
        "gates": digest.get("gates") or [],
        "counters": counters,
        "warning_list": list(digest.get("warnings") or [])[:40],
    })
    return compact, detail


def may_replace(existing: Optional[Dict[str, Any]]) -> bool:
    """A loop record replaces only another loop record - a census or task
    record for the same session is the richer one."""
    return not existing or str(existing.get("source") or "") == SOURCE
