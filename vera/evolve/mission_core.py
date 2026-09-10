"""Mission control's one table: a row per EVENT, built from the pages it
replaced (Master, Activity, Errors) and the live theatres it folds in.

Activity was the audit log (every Loop Lab action), Errors the work-queue
of problems (error -> suggested fix -> approved -> committed), Master a
glance at what needs a person (gated pipelines, active items, recent
gates) over the same activity. An event is the thing all of them list -
something happened, at a time, by someone, and it went well or not - so
it is the row: an audit ACTION, an ERROR in the queue, a GATE (a unit-test
run). Above the table, counts() says what needs a person now.

Pure: no I/O. mission_capabilities gathers the readers and hands their rows
in - the same shape of module as work_core, ship_core and agents_core.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

KIND_ACTION = "action"
KIND_ERROR = "error"
KIND_GATE = "gate"
KINDS = (KIND_ACTION, KIND_ERROR, KIND_GATE)

ERROR_STATES = ("new", "suggested", "approved", "applied", "dismissed")
OPEN_ERROR_STATES = ("new", "suggested", "approved")
ACTIVE_LANES = ("in_progress", "needs_review", "review", "ready", "blocked", "in_progress_vera")

# An action's family, from its name: the table filters on it and colours it.
_FAMILIES = ("pipeline", "bleeding_edge", "sandbox", "unittest", "census", "board", "config", "docker",
             "improve", "suite", "task", "variant", "worktree", "repo", "editq", "errors")


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def family_of(action: str) -> str:
    a = _s(action).strip().lower()
    for f in _FAMILIES:
        if a == f or a.startswith(f + "."):
            return f
    return a.split(".")[0] if a else ""


def who_of(entry: Dict[str, Any]) -> str:
    """Who did it: the controller when recorded, else the host that ran it."""
    c = _s(entry.get("controller") or entry.get("agent"))
    if c:
        return c
    by = entry.get("by")
    if isinstance(by, dict):
        host = _s(by.get("host"))
        return (host + (" (dev)" if by.get("dev") else "")) if host else _s(by.get("name"))
    return _s(by)


def ref_of(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The record an audit entry is about, when it names one."""
    a = _s(entry.get("action"))
    if entry.get("id") and (a.startswith("pipeline") or a.startswith("bleeding_edge") or entry.get("kind") == "code"):
        return {"kind": "pipeline", "id": _s(entry.get("id"))}
    if entry.get("branch"):
        return {"kind": "branch", "id": _s(entry.get("branch"))}
    if entry.get("run_id"):
        return {"kind": "run", "id": _s(entry.get("run_id"))}
    return None


def action_event(e: Dict[str, Any]) -> Dict[str, Any]:
    action = _s(e.get("action"))
    ok = e.get("ok")
    return {
        "id": "a:" + _s(e.get("ts")) + ":" + action, "kind": KIND_ACTION, "ts": _s(e.get("ts")),
        "action": action, "family": family_of(action), "summary": _s(e.get("summary")),
        "ok": (bool(ok) if ok is not None else None), "problem": ok is False,
        "who": who_of(e), "ver": _s((e.get("by") or {}).get("ver")) if isinstance(e.get("by"), dict) else "",
        "level": _s(e.get("level")), "ref": ref_of(e), "state": "",
        "branch": _s(e.get("branch")), "raw": e,
    }


def error_event(it: Dict[str, Any]) -> Dict[str, Any]:
    meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
    sug = it.get("suggestion") if isinstance(it.get("suggestion"), dict) else {}
    state = _s(it.get("state"))
    return {
        "id": "e:" + _s(it.get("id")), "kind": KIND_ERROR, "ts": _s(it.get("last_seen") or it.get("updated_at") or it.get("ts") or it.get("first_seen")),
        "action": "error." + _s(it.get("source") or "unknown"), "family": "errors", "summary": _s(it.get("title")),
        "ok": state in ("applied", "dismissed"), "problem": state in OPEN_ERROR_STATES,
        "who": _s(it.get("source")), "ver": "", "level": _s(meta.get("severity")), "state": state,
        "ref": ({"kind": "pipeline", "id": _s(it.get("pipeline_id"))} if it.get("pipeline_id")
                else ({"kind": "run", "id": _s(meta.get("run_id"))} if meta.get("run_id") else None)),
        "error_id": _s(it.get("id")), "count": int(it.get("count") or 1), "suggestion": _s(sug.get("suggestion")),
        "remediation": bool(sug.get("remediation_id") or meta.get("remediation_id")),
        "remediation_result": _s(it.get("remediation_result")), "branch": "", "raw": it,
    }


def gate_event(r: Dict[str, Any]) -> Dict[str, Any]:
    ok = bool(r.get("ok"))
    return {
        "id": "g:" + _s(r.get("ts")) + ":" + _s(r.get("branch")), "kind": KIND_GATE, "ts": _s(r.get("ts")),
        "action": "gate." + (_s(r.get("markers")) or "tests"), "family": "unittest",
        "summary": _s(r.get("summary")) or ("%s passed, %s failed" % (r.get("passed") or 0, r.get("failed") or 0)),
        "ok": ok, "problem": not ok, "who": "", "ver": "", "level": "", "state": "",
        "ref": ({"kind": "pipeline", "id": _s(r.get("pipeline_id"))} if r.get("pipeline_id")
                else ({"kind": "branch", "id": _s(r.get("branch"))} if r.get("branch") else None)),
        "branch": _s(r.get("branch")), "passed": int(r.get("passed") or 0), "failed": int(r.get("failed") or 0),
        "total": int(r.get("total") or 0), "raw": r,
    }


def event_rows(audit: Iterable[Dict[str, Any]], errors: Iterable[Dict[str, Any]] = (),
               tests: Iterable[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Every event, newest first. An open error (new / suggested / approved)
    leads whatever its time: it is the thing waiting on a person."""
    out: List[Dict[str, Any]] = []
    for e in audit or []:
        if isinstance(e, dict) and (e.get("action") or e.get("summary")):
            out.append(action_event(e))
    for it in errors or []:
        if isinstance(it, dict) and it.get("id"):
            out.append(error_event(it))
    for r in tests or []:
        if isinstance(r, dict) and r.get("ts"):
            out.append(gate_event(r))
    out.sort(key=lambda x: x["ts"], reverse=True)
    out.sort(key=lambda x: 0 if (x["kind"] == KIND_ERROR and x["state"] in OPEN_ERROR_STATES) else 1)
    return out


def filter_events(rows: Iterable[Dict[str, Any]], *, kind: str = "", family: str = "", text: str = "",
                  problems: bool = False, who: str = "", hide_exec: bool = False) -> List[Dict[str, Any]]:
    q = (text or "").strip().lower()
    out = []
    for r in rows:
        if kind and r.get("kind") != kind:
            continue
        if family and r.get("family") != family:
            continue
        if problems and not r.get("problem"):
            continue
        if who and (who.lower() not in _s(r.get("who")).lower()):
            continue
        if hide_exec and r.get("action") == "sandbox.exec":
            continue
        if q:
            hay = " ".join([_s(r.get("action")), _s(r.get("summary")), _s(r.get("who")), _s(r.get("branch")),
                            _s((r.get("ref") or {}).get("id")), _s(r.get("suggestion"))]).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def counts(pipelines: Iterable[Dict[str, Any]], items: Iterable[Dict[str, Any]], errors: Iterable[Dict[str, Any]],
           tests: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What Master's four cards counted, from the same reads: pipelines that
    need a promote (gated or reviewed, undecided), active board items, the
    errors by state, the last gates, what is live."""
    ps = [p for p in pipelines if isinstance(p, dict)]
    its = [i for i in items if isinstance(i, dict)]
    errs = [e for e in errors if isinstance(e, dict)]
    ts = [t for t in tests if isinstance(t, dict)]
    needs = [p for p in ps if _s(p.get("decision")).lower() not in ("promoted", "rolled_back")
             and (p.get("review_requested") or p.get("gate_passed") is True)]
    by_state: Dict[str, int] = {}
    for e in errs:
        st = _s(e.get("state")) or "new"
        by_state[st] = by_state.get(st, 0) + 1
    recent = ts[:12]
    return {
        "needs_promotion": len(needs), "needs_promotion_ids": [_s(p.get("id")) for p in needs[:12]],
        "live_pipelines": sum(1 for p in ps if p.get("live")),
        "active_items": sum(1 for i in its if _s(i.get("lane")) in ACTIVE_LANES),
        "errors": by_state, "errors_open": sum(v for k, v in by_state.items() if k in OPEN_ERROR_STATES),
        "gates_recent": len(recent), "gates_red": sum(1 for t in recent if not t.get("ok")),
        "last_gate": ({"ok": bool(recent[0].get("ok")), "branch": _s(recent[0].get("branch")), "ts": _s(recent[0].get("ts")),
                       "summary": _s(recent[0].get("summary"))} if recent else None),
    }


def summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    kinds: Dict[str, int] = {}
    fams: Dict[str, int] = {}
    n = problems = 0
    for r in rows:
        n += 1
        kinds[r.get("kind") or ""] = kinds.get(r.get("kind") or "", 0) + 1
        fams[r.get("family") or ""] = fams.get(r.get("family") or "", 0) + 1
        if r.get("problem"):
            problems += 1
    return {"count": n, "kinds": kinds, "families": fams, "problems": problems}
