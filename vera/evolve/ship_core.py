"""The Ship page's one table: a row per BRANCH, built from the five views it
replaced (CI/CD, Review, Sources, Sandbox, Unit tests).

Those pages each listed the same branches from a different store: the
pipeline list (what CI did to it), the review queue (a pipeline holding for
a decision), the sandbox registry (a container running its code), the
unit-test history (its runs going red and green), the edges list (the
integration branches main advances to). A branch is the thing that moves
through all of them, so it is the row, and the stores are its columns.

Pure: no I/O, no Redis. ship_capabilities gathers the five readers and
hands their rows in; the Work page's work_core is the same shape of module
for the driver runs.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

ROLE_MAIN = "main"
ROLE_EDGE = "edge"
ROLE_MIRROR = "mirror"
ROLE_FEATURE = "feature"
ROLES = (ROLE_MAIN, ROLE_EDGE, ROLE_MIRROR, ROLE_FEATURE)

# One word for where a branch stands. Ordered: the table sorts by it.
STAGE_LIVE = "live"        # a pipeline is running on it now
STAGE_REVIEW = "review"    # a pipeline is holding for a promote / rollback call
STAGE_RED = "red"          # its last gate or test run failed and nothing landed since
STAGE_MERGED = "merged"    # its change landed (promoted, or merged into an edge)
STAGE_IDLE = "idle"        # nothing pending: a sandbox with no pipeline, a bare branch
STAGES = (STAGE_LIVE, STAGE_REVIEW, STAGE_RED, STAGE_MERGED, STAGE_IDLE)

UNDECIDED = ("pending", "held")
TERMINAL = ("promoted", "rolled_back")


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _ts(v: Any) -> str:
    """A sortable time string ('' sorts first, so max() prefers a real one)."""
    return _s(v)


def branch_role(name: str, *, main: str = "main", edges: Iterable[str] = ()) -> str:
    if not name:
        return ROLE_FEATURE
    if name == (main or "main"):
        return ROLE_MAIN
    if name in set(edges or ()):
        return ROLE_EDGE
    if name.startswith("loop-lab/"):
        return ROLE_MIRROR
    return ROLE_FEATURE


def pipeline_cell(p: Dict[str, Any]) -> Dict[str, Any]:
    """The compact pipeline record the row carries (the list row, not the
    full stage record - openPipe fetches that)."""
    return {
        "id": _s(p.get("id")), "kind": _s(p.get("kind") or ""), "profile": _s(p.get("profile") or ""),
        "status": _s(p.get("status") or ""), "decision": _s(p.get("decision") or ""),
        "gate_passed": p.get("gate_passed"), "gate_delta": p.get("gate_delta"),
        "baseline_score": p.get("baseline_score"), "candidate_score": p.get("candidate_score"),
        "live": bool(p.get("live")), "adopted": bool(p.get("adopted")),
        "review_requested": bool(p.get("review_requested")),
        "controller": _s(p.get("controller") or ""), "session_id": _s(p.get("session_id") or ""),
        "repo": _s(p.get("repo") or "vera"),
        "created_at": _s(p.get("created_at") or ""), "ended_at": _s(p.get("ended_at") or ""),
    }


def sandbox_cell(s: Dict[str, Any]) -> Dict[str, Any]:
    """The sandbox record as the registry gave it (the row's expanded detail
    draws every control the Sandbox page had from it), with the fields the
    table reads normalised."""
    cell = dict(s)
    cell.update({
        "name": _s(s.get("name")), "role": _s(s.get("role") or ""), "port": s.get("port"),
        "redis_db": s.get("redis_db"), "running": bool(s.get("running")), "paused": bool(s.get("paused")),
        "pinned": bool(s.get("pinned")), "state": _s(s.get("state") or ""),
        "dirty": s.get("dirty"), "merged_to_bleeding_edge": s.get("merged_to_bleeding_edge"),
        "head_commit": _s(s.get("head_commit") or "")[:12], "owner": _s(s.get("owner") or ""),
        "session_id": _s(s.get("session_id") or ""), "last_activity": _s(s.get("last_activity") or ""),
        "workplan": s.get("workplan") if isinstance(s.get("workplan"), dict) and s.get("workplan") else None,
    })
    return cell


def _run_key(r: Dict[str, Any]) -> str:
    return _ts(r.get("ts"))


def tests_cell(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The branch's test story: its latest run, how many runs it took, how
    many of them were red, and the test-count delta of the latest (a green
    on fewer tests is the regression the gate reports as PASS)."""
    if not runs:
        return None
    ordered = sorted(runs, key=_run_key)
    last = ordered[-1]
    prev = ordered[-2] if len(ordered) > 1 else None
    total = int(last.get("total") or 0)
    delta = (total - int(prev.get("total") or 0)) if prev is not None else 0
    return {
        "ts": _s(last.get("ts")), "ok": bool(last.get("ok")), "passed": int(last.get("passed") or 0),
        "failed": int(last.get("failed") or 0), "total": total, "markers": _s(last.get("markers") or ""),
        "summary": _s(last.get("summary") or ""), "pipeline_id": _s(last.get("pipeline_id") or ""),
        "runs_n": len(ordered), "red_runs": sum(1 for r in ordered if not r.get("ok")),
        "delta": delta, "fewer": bool(last.get("ok")) and delta < 0,
    }


def edge_cell(e: Dict[str, Any]) -> Dict[str, Any]:
    c = e.get("container") if isinstance(e.get("container"), dict) else {}
    return {"name": _s(e.get("name")), "branch": _s(e.get("branch") or e.get("name")), "default": bool(e.get("default")),
            "exists": e.get("exists", True),
            "head": _s(e.get("head") or "")[:12], "main_state": _s(e.get("main_state") or ""),
            "description": _s(e.get("description") or ""), "mirror_head": _s(e.get("mirror_head") or "")[:12],
            "container": {"name": _s(c.get("name")), "running": bool(c.get("running")), "port": c.get("port"),
                          "redis_db": c.get("redis_db"), "pinned": bool(c.get("pinned"))} if c else None}


def stage_of(row: Dict[str, Any]) -> str:
    p = row.get("pipeline") or {}
    t = row.get("tests") or {}
    if row.get("live"):
        return STAGE_LIVE
    if row.get("pending_n") or row.get("review_requested"):
        return STAGE_REVIEW
    if p and p.get("decision") not in TERMINAL and p.get("gate_passed") is False:
        return STAGE_RED
    # A failed test run is red until something lands after it.
    landed_after = bool(p and p.get("decision") == "promoted"
                        and _ts(p.get("ended_at") or p.get("created_at")) > _ts(t.get("ts")))
    if t and not t.get("ok") and not landed_after:
        return STAGE_RED
    if row.get("merged"):
        return STAGE_MERGED
    return STAGE_IDLE


def branch_rows(pipelines: Iterable[Dict[str, Any]], sandboxes: Iterable[Dict[str, Any]],
                test_runs: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, Any]],
                git_status: Optional[Dict[str, Any]] = None, *, main: str = "main") -> List[Dict[str, Any]]:
    """One row per branch, live first, then awaiting a decision, then red,
    then newest activity first."""
    git_status = git_status or {}
    # The branch prod's checkout is on IS main for this table (git status says).
    main = _s(git_status.get("branch")) or (main or "main")
    by_branch: Dict[str, Dict[str, Any]] = {}

    def row(name: str) -> Dict[str, Any]:
        r = by_branch.get(name)
        if r is None:
            r = by_branch[name] = {
                "branch": name, "role": ROLE_FEATURE, "repo": "vera", "stage": STAGE_IDLE,
                "pipeline": None, "pipelines": [], "pipelines_n": 0, "pending_n": 0, "live": False,
                "review_requested": False, "sandbox": None, "tests": None, "edge": None,
                "merged": False, "head": "", "dirty": None, "owner": "", "session_id": "",
                "last_activity": "", "main": False,
            }
        return r

    edge_names = {_s(e.get("branch") or e.get("name")) for e in (edges or []) if isinstance(e, dict)}
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        name = _s(e.get("branch") or e.get("name"))
        if not name:
            continue
        r = row(name)
        r["edge"] = edge_cell(e)
        r["head"] = r["head"] or r["edge"]["head"]
        # Released = main is at this edge: the change landed as far as it goes.
        r["merged"] = r["merged"] or r["edge"]["main_state"] == "released"

    if main:
        r = row(main)
        r["main"] = True
        r["dirty"] = bool(git_status.get("dirty")) if "dirty" in git_status else None
    for b in git_status.get("branches") or []:
        if _s(b):
            row(_s(b))

    for p in pipelines or []:
        if not isinstance(p, dict) or not _s(p.get("branch")):
            continue
        r = row(_s(p.get("branch")))
        r["pipelines"].append(pipeline_cell(p))
    for r in by_branch.values():
        ps = sorted(r["pipelines"], key=lambda x: _ts(x.get("created_at")), reverse=True)
        r["pipelines"] = ps
        r["pipelines_n"] = len(ps)
        r["pipeline"] = ps[0] if ps else None
        r["live"] = any(x["live"] for x in ps)
        # An undecided pipeline older than the branch's latest promote / rollback
        # was superseded by it (the branch was adopted again); it is not a
        # decision anyone still owes.
        decided_at = next((_ts(x["created_at"]) for x in ps if x["decision"] in TERMINAL), "")
        for x in ps:
            x["superseded"] = x["decision"] in UNDECIDED and _ts(x["created_at"]) < decided_at
        r["pending_n"] = sum(1 for x in ps if x["decision"] in UNDECIDED and not x["live"] and not x["superseded"])
        r["review_requested"] = any(x["review_requested"] and x["decision"] not in TERMINAL and not x["superseded"]
                                    for x in ps)
        if ps:
            r["repo"] = ps[0]["repo"] or r["repo"]
            r["owner"] = r["owner"] or ps[0]["controller"]
            r["session_id"] = r["session_id"] or ps[0]["session_id"]
            r["merged"] = r["merged"] or ps[0]["decision"] == "promoted"

    for s in sandboxes or []:
        if not isinstance(s, dict) or not _s(s.get("branch")):
            continue
        r = row(_s(s.get("branch")))
        cell = sandbox_cell(s)
        cur = r["sandbox"]
        # Several containers on one branch: the one running (the primary first) is the row's.
        if cur is None or (cell["running"] and not cur["running"]) or (cell["role"] == "primary" and cur["role"] != "primary"):
            r["sandbox"] = cell
    for r in by_branch.values():
        sb = r["sandbox"]
        if sb:
            r["head"] = r["head"] or sb["head_commit"]
            r["dirty"] = sb["dirty"] if sb["dirty"] is not None else r["dirty"]
            r["owner"] = r["owner"] or sb["owner"]
            r["session_id"] = r["session_id"] or sb["session_id"]
            if sb["merged_to_bleeding_edge"] is True:
                r["merged"] = True

    runs_by: Dict[str, List[Dict[str, Any]]] = {}
    for t in test_runs or []:
        if isinstance(t, dict) and _s(t.get("branch")):
            runs_by.setdefault(_s(t.get("branch")), []).append(t)
    for name, runs in runs_by.items():
        row(name)["tests"] = tests_cell(runs)

    out = []
    for name, r in by_branch.items():
        r["role"] = ROLE_MAIN if r["main"] else branch_role(name, main=main, edges=edge_names)
        times = [_ts(r["pipeline"] and (r["pipeline"].get("ended_at") or r["pipeline"].get("created_at"))),
                 _ts(r["sandbox"] and r["sandbox"].get("last_activity")),
                 _ts(r["tests"] and r["tests"].get("ts"))]
        r["last_activity"] = max(times)
        r["stage"] = stage_of(r)
        out.append(r)
    # Newest activity first (a row with no time last), then the bands in
    # order - the second sort is stable, so each band keeps newest-first.
    out.sort(key=lambda r: r["last_activity"], reverse=True)
    out.sort(key=_band)
    return out


def _band(r: Dict[str, Any]) -> int:
    """The trunk first (main, then the edges it releases from); then the
    rows that need a person - live, awaiting a decision, red; merged and
    idle share the band below, by recency."""
    if r.get("role") == ROLE_MAIN:
        return 0
    if r.get("role") == ROLE_EDGE:
        return 1
    return {STAGE_LIVE: 2, STAGE_REVIEW: 3, STAGE_RED: 4}.get(r.get("stage") or "", 5)


def filter_rows(rows: Iterable[Dict[str, Any]], *, role: str = "", stage: str = "", text: str = "",
                hide_merged: bool = False) -> List[Dict[str, Any]]:
    q = (text or "").strip().lower()
    out = []
    for r in rows:
        if role and r.get("role") != role:
            continue
        if stage and r.get("stage") != stage:
            continue
        if hide_merged and r.get("stage") == STAGE_MERGED:
            continue
        if q:
            hay = " ".join([_s(r.get("branch")), _s(r.get("owner")), _s(r.get("session_id")),
                            _s((r.get("sandbox") or {}).get("name")), _s(r.get("head")),
                            " ".join(_s(p.get("id")) for p in r.get("pipelines") or [])]).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    st: Dict[str, int] = {s: 0 for s in STAGES}
    ro: Dict[str, int] = {r: 0 for r in ROLES}
    n = 0
    sandboxes = running = 0
    for r in rows:
        n += 1
        st[r.get("stage") or STAGE_IDLE] = st.get(r.get("stage") or STAGE_IDLE, 0) + 1
        ro[r.get("role") or ROLE_FEATURE] = ro.get(r.get("role") or ROLE_FEATURE, 0) + 1
        if r.get("sandbox"):
            sandboxes += 1
            if r["sandbox"].get("running"):
                running += 1
    return {"count": n, "stages": st, "roles": ro, "sandboxes": sandboxes, "sandboxes_running": running}
