"""ci.* and loop.ci.* - automated development work as pictures.

The pictures Loop Lab draws (status matrix, race to green, tests across runs,
a run's track, a board, the fleet, two runs compared) as capabilities, so an
agent - or a person in chat - can ask for one and have it drawn on the canvas.
Every answer is one ``kind: "ci"`` payload (ci_view_core); the canvas maps each
capability to its widget (widget_cap_output.CAP_HINTS), and the Loop Lab page
draws the same payload with the same renderers.

  ci.*       generic code work: gate runs of ANY registered repo's branches,
             driven by any agent (claude · codex · vera · user)
  loop.ci.*  the agentic loop: one run's steps, calls and gate rounds, or many
             runs racing on the same goal

Nothing here truncates on its own. ``cols`` asks for the newest N cells per
lane and the payload then counts what it left out; ``ci.run`` returns a run
whole - every failing test, every pipeline step's full text, the board items
and conversations linked to it.

Loaded after evolve/ so it reaches the stores through sys.modules (the loader
registers modules by bare filename).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import types
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import ci_view_core as cv
    from Vera.vera.evolve.ttl_cache import TTLCache
except ImportError:                                   # pragma: no cover
    from vera.evolve import ci_view_core as cv
    from vera.evolve.ttl_cache import TTLCache

log = logging.getLogger("vera.evolve.ci")

#: how far back the pictures read by default: the whole kept history
HISTORY_N = 5000
PIPELINES_N = 1000

#: The command centre asks for three pictures at once (pulse, fleet, the open
#: tab) and a live page re-asks on every gate event: each read the whole kept
#: history and the pipeline list again. The stores change at gate speed, not
#: request speed, so they are read once per few seconds and shared (concurrent
#: callers wait for the one read in flight).
_READS = TTLCache(4.0, max_entries=8)


def _ev():
    return sys.modules.get("evolve_capabilities")


async def _call(name: str, **kw) -> Any:
    ev = _ev()
    if ev is None:
        return {"error": "evolve not loaded"}
    return await ev._call(name, **kw)


def _orch():
    return sys.modules.get("Vera.vera.capability_orchestration") or sys.modules.get("capability_orchestration")


def _in_sandbox() -> bool:
    o = _orch()
    return bool(o is not None and getattr(o, "_READ_THROUGH_URL", ""))


async def _read(name: str, **kw) -> Any:
    """A reading of the estate's CI state. In a dev sandbox it comes from PROD: a sandbox has its own private,
    empty Redis, so reading it locally drew every picture empty (27 Sep: "none of the tabs return results" -
    evolve.unittest.history read through on its own, but ci.* read the store directly, and board.* is not a
    read-through group). Prod answers through the same door the dashboard's reads use; when it cannot, the
    sandbox answers for itself. Outside a sandbox this is the local capability."""
    o = _orch()
    up = getattr(o, "_upstream_read", None) if (o is not None and getattr(o, "_READ_THROUGH_URL", "")) else None
    if up is not None:
        try:
            r = await up(name, kw)
        except Exception:                             # pragma: no cover
            r = None
        if isinstance(r, dict) and not (r.get("error") and len(r) <= 3):
            return r
    return await _call(name, **kw)


async def _history(limit: int = HISTORY_N) -> List[Dict[str, Any]]:
    ev = _ev()
    n = max(1, min(HISTORY_N, int(limit or HISTORY_N)))

    async def read():
        if _in_sandbox() or ev is None:
            res = await _read("evolve.unittest.history", limit=n)
            return list((res or {}).get("runs") or []) if isinstance(res, dict) else []
        try:
            return await ev._get_unittest_history(n)
        except Exception as e:                        # pragma: no cover
            log.info("ci: history read failed: %s", e)
            return []
    return list(await _READS.get(("history", n), read))


async def _pipelines(limit: int = PIPELINES_N) -> List[Dict[str, Any]]:
    async def read():
        res = await _read("evolve.pipeline.list", limit=limit)
        return list((res or {}).get("pipelines") or []) if isinstance(res, dict) else []
    return list(await _READS.get(("pipelines", int(limit)), read))


def _attribute(rows: List[Dict[str, Any]], pipes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Runs recorded before rows carried their controller get it from their
    pipeline (by id, else the branch's newest pipeline) — so old history is
    split by agent too, not lumped as unattributed."""
    by_id = {str(p.get("id")): p for p in pipes}
    by_branch: Dict[str, Dict[str, Any]] = {}
    for p in pipes:
        by_branch.setdefault(str(p.get("branch") or ""), p)
    out = []
    for r in rows:
        if r.get("controller"):
            out.append(r)
            continue
        p = by_id.get(str(r.get("pipeline_id") or "")) or by_branch.get(str(r.get("branch") or ""))
        if p:
            r = dict(r)
            r["controller"] = p.get("controller") or ""
            r["via"] = p.get("via") or ""
            if not r.get("session_id"):
                r["session_id"] = p.get("session_id") or ""
        out.append(r)
    return out


_FILTER_DOC = ("Filters (all optional): branch (exact; 'feat/' or 'x*' = prefix), q (text over "
               "branch, summary, pipeline id, failing test ids), controller (claude|codex|vera|user), "
               "status (pass|fail|error|running|red), since/until (ISO), markers (e.g. critical), "
               "repo, session_id.")


async def _filtered(branch: str = "", q: str = "", controller: str = "", status: str = "",
                    since: str = "", until: str = "", markers: str = "", repo: str = "",
                    session_id: str = "", limit: int = HISTORY_N):
    rows = await _history(limit)
    pipes = await _pipelines()
    rows = _attribute(rows, pipes)
    rows = cv.filter_rows(rows, branch=branch, q=q, controller=controller, status=status,
                          since=since, until=until, markers=markers, repo=repo,
                          session_id=session_id)
    return rows, pipes


# ─────────────────────────────────────────────────────────────────────────────
# ci.* — generic code work
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ci.matrix", memory="off", silent=True,
    http_method="GET", http_path="/ci/matrix", http_tags=["ci", "evolve"],
    description=("STATUS MATRIX of automated code work: one lane per branch (or controller, day, "
                 "markers), one cell per gate/test run, oldest→newest, each cell carrying its "
                 "counts, failing test ids, pipeline, controller and session. Drawn on the canvas as "
                 "the status-matrix widget. Inputs: group (branch|controller|day|markers), cols "
                 "(int, 0 = every run — else the newest N per lane, with the rest counted in "
                 "`hidden`), order (recent|red|name). " + _FILTER_DOC +
                 " Output: {kind:'ci', view:'matrix', lanes:[{id, name, cells, hidden, race, "
                 "state}], summary}."))
async def ci_matrix(group: str = "branch", cols: int = 0, order: str = "recent",
                    branch: str = "", q: str = "", controller: str = "", status: str = "",
                    since: str = "", until: str = "", markers: str = "", repo: str = "",
                    session_id: str = "", trace_id=None):
    rows, _ = await _filtered(branch, q, controller, status, since, until, markers, repo, session_id)
    return cv.matrix(rows, group=group, cols=max(0, int(cols or 0)), order=order,
                     source="evolve.unittest.history")


@capability(
    "ci.race", memory="off", silent=True,
    http_method="GET", http_path="/ci/race", http_tags=["ci", "evolve"],
    description=("RACE TO GREEN: every lane's red→green laps (runs it took, seconds it took), "
                 "lanes still red first, then by how quickly they last came good. Drawn as the "
                 "race-green widget. Inputs: group (branch|controller|day), cols. " + _FILTER_DOC +
                 " Output: {kind:'ci', view:'race', lanes:[{name, cells, race:{state, streak, laps, "
                 "red_runs, best_attempts}}], summary:{median_attempts, median_seconds_to_green, …}}."))
async def ci_race(group: str = "branch", cols: int = 0, branch: str = "", q: str = "",
                  controller: str = "", status: str = "", since: str = "", until: str = "",
                  markers: str = "", repo: str = "", session_id: str = "", trace_id=None):
    rows, _ = await _filtered(branch, q, controller, status, since, until, markers, repo, session_id)
    return cv.race(rows, group=group, cols=max(0, int(cols or 0)), source="evolve.unittest.history")


@capability(
    "ci.tests", memory="off", silent=True,
    http_method="GET", http_path="/ci/tests", http_tags=["ci", "evolve"],
    description=("TESTS ACROSS RUNS: every test that failed in any gate run, against every run — "
                 "classified broken (red now), flaky (flipped twice+), fixed (red before, green now). "
                 "A run whose failure list was cut short shows `unknown`, never a false pass. Drawn "
                 "as the test-grid widget. Inputs: cols (newest N runs; 0 = all). " + _FILTER_DOC +
                 " Output: {kind:'ci', view:'tests', columns:[run], tests:[{id, cells, class, "
                 "flips, description}], summary}."))
async def ci_tests(cols: int = 0, branch: str = "", q: str = "", controller: str = "",
                   status: str = "", since: str = "", until: str = "", markers: str = "",
                   repo: str = "", session_id: str = "", trace_id=None):
    rows, _ = await _filtered(branch, q, controller, status, since, until, markers, repo, session_id)
    return cv.test_grid(rows, cols=max(0, int(cols or 0)), source="evolve.unittest.history")


def _pick(rows: List[Dict[str, Any]], ref: str) -> Optional[Dict[str, Any]]:
    """A run by pipeline id, timestamp, or 'branch@N' (N-th newest, 0-based)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if "@" in ref:
        b, _, n = ref.rpartition("@")
        mine = [r for r in cv.oldest_first(rows) if r.get("branch") == b]
        try:
            return list(reversed(mine))[int(n)]
        except (ValueError, IndexError):
            return None
    for r in reversed(cv.oldest_first(rows)):
        if ref in (str(r.get("ts")), str(r.get("pipeline_id"))):
            return r
    return None


@capability(
    "ci.compare", memory="off", silent=True,
    http_method="GET", http_path="/ci/compare", http_tags=["ci", "evolve"],
    description=("Compare two gate runs: tests FIXED, BROKEN, and STILL failing, the count deltas "
                 "and the time between. Drawn as the run-compare widget. Inputs: a, b (each a run's "
                 "ts, its pipeline id, or 'branch@N' = the branch's N-th newest run), or branch "
                 "alone (its newest run against the one before). Output: {kind:'ci', view:'compare', "
                 "a, b, delta, fixed, broken, still, partial}."))
async def ci_compare(a: str = "", b: str = "", branch: str = "", trace_id=None):
    rows = await _history()
    rows = _attribute(rows, await _pipelines())
    if branch and not (a or b):
        a, b = f"{branch}@1", f"{branch}@0"
    ra, rb = _pick(rows, a), _pick(rows, b)
    if ra is None or rb is None:
        return {"error": "run not found: " + ", ".join(x for x, r in ((a, ra), (b, rb)) if r is None)}
    if str(ra.get("ts")) > str(rb.get("ts")):
        ra, rb = rb, ra
    return cv.compare(ra, rb)


@capability(
    "ci.pulse", memory="off", silent=True,
    http_method="GET", http_path="/ci/pulse", http_tags=["ci", "evolve"],
    description=("CI PULSE: pass rate and runs per day (or hour), the current streak, runs split by "
                 "agent (claude · codex · vera · user), median attempts and seconds to green. Drawn "
                 "as the ci-pulse widget. Inputs: buckets (day|hour). " + _FILTER_DOC +
                 " Output: {kind:'ci', view:'pulse', series:[{t, runs, pass, red, rate, tests}], "
                 "summary, latest}."))
async def ci_pulse(buckets: str = "day", branch: str = "", q: str = "", controller: str = "",
                   status: str = "", since: str = "", until: str = "", markers: str = "",
                   repo: str = "", session_id: str = "", trace_id=None):
    rows, _ = await _filtered(branch, q, controller, status, since, until, markers, repo, session_id)
    return cv.pulse(rows, buckets=buckets, source="evolve.unittest.history")


@capability(
    "ci.track", memory="off", silent=True,
    http_method="GET", http_path="/ci/track", http_tags=["ci", "evolve"],
    description=("One pipeline as a TRACK of stages (begin → commit → compile → tests → review → "
                 "promote), each with its steps' full detail. Drawn as the run-track widget. Input: "
                 "id (pipeline id) or branch (its newest pipeline). Output: {kind:'ci', view:'track', "
                 "pipeline, controller, stages, commits, changed_files}."))
async def ci_track(id: str = "", branch: str = "", trace_id=None):
    if not id and branch:
        p = next((p for p in await _pipelines() if p.get("branch") == branch), None)
        id = (p or {}).get("id", "")
    if not id:
        return {"error": "id or branch required"}
    res = await _read("evolve.pipeline.get", id=id)
    p = (res or {}).get("pipeline") if isinstance(res, dict) else None
    if not p:
        return {"error": f"pipeline not found: {id}"}
    return cv.track(p)


@capability(
    "ci.board", memory="off", silent=True,
    http_method="GET", http_path="/ci/board", http_tags=["ci", "board"],
    description=("The WORK BOARD as columns, each card joined to its pipeline's gate state and "
                 "decision. Drawn as the ci-board widget. Inputs: include_done (bool=true), label, "
                 "agent, repo, text. Output: {kind:'ci', view:'board', columns:[{name, items}], "
                 "summary}."))
async def ci_board(include_done: bool = True, label: str = "", agent: str = "", repo: str = "",
                   text: str = "", trace_id=None):
    res = await _read("board.items", label=label, agent=agent, repo=repo, text=text)
    items = (res or {}).get("items") or [] if isinstance(res, dict) else []
    inc = include_done if isinstance(include_done, bool) else str(include_done).lower() not in ("0", "false", "no")
    return cv.board(items, await _pipelines(), include_done=inc)


async def _loop_sessions(limit: int = 100) -> List[Dict[str, Any]]:
    o = _orch()
    url = str(getattr(o, "_READ_THROUGH_URL", "") or "") if o is not None else ""
    if url.endswith("/mcp/call"):                     # a sandbox: prod's loop history, not the sandbox's empty one
        try:
            import httpx
            async with httpx.AsyncClient(verify=False, timeout=20) as c:
                r = await c.get(url[:-len("/mcp/call")] + "/workshop/agent_loop/sessions",
                                params={"limit": str(max(1, min(100, limit)))})
            if r.status_code == 200:
                ss = (r.json() or {}).get("sessions") or []
                if ss:
                    return list(ss)
        except Exception as e:                        # pragma: no cover
            log.debug("ci: prod loop sessions: %s", e)
    mod = sys.modules.get("dag_workshop_capabilities")
    fn = getattr(mod, "workshop_loop_sessions", None) if mod else None
    if fn is None:
        return []
    try:
        res = await fn(types.SimpleNamespace(query_params={"limit": str(max(1, min(100, limit)))}))
        return list((res or {}).get("sessions") or [])
    except Exception as e:                            # pragma: no cover
        log.info("ci: loop sessions read failed: %s", e)
        return []


async def _conversations() -> List[Dict[str, Any]]:
    """Pipelines that name a session, as conversations keyed by branch."""
    return [{"branch": p.get("branch"), "session_id": p.get("session_id"),
             "controller": p.get("controller"), "pipeline": p.get("id")}
            for p in await _pipelines() if p.get("session_id")]


@capability(
    "ci.fleet", memory="off", silent=True,
    http_method="GET", http_path="/ci/fleet", http_tags=["ci", "evolve"],
    description=("The FLEET: every sandbox with its branch's latest pipeline (gate, decision), its "
                 "owner and how many conversations drove the branch. Drawn as the ci-fleet widget. "
                 "Output: {kind:'ci', view:'fleet', cards, summary}."))
async def ci_fleet(trace_id=None):
    sb = await _read("evolve.sandbox.list")
    boxes = (sb or {}).get("sandboxes") or [] if isinstance(sb, dict) else []
    return cv.fleet(boxes, await _pipelines(), await _conversations())


@capability(
    "ci.run", memory="off", silent=True,
    http_method="GET", http_path="/ci/run", http_tags=["ci", "evolve"],
    description=("ONE RUN, WHOLE — the drill-down behind every matrix cell. Nothing clipped: every "
                 "failing test with its description, the pipeline's every step with its full text "
                 "(as a track), the branch's whole lane of runs and its race, the board items linked "
                 "by pipeline or branch, and the conversation (session) that drove it. Input: ref "
                 "(a run's ts, its pipeline id, or 'branch@N'), or pipeline (id). Output: {kind:'ci', "
                 "view:'run', run, track, lane, board_items, conversation}."))
async def ci_run(ref: str = "", pipeline: str = "", trace_id=None):
    pipes = await _pipelines()
    rows = _attribute(await _history(), pipes)
    run = _pick(rows, ref or pipeline) if (ref or pipeline) else None
    pid = pipeline or (run or {}).get("pipeline_id") or ""
    branch = (run or {}).get("branch") or ""
    p = None
    if pid:
        res = await _read("evolve.pipeline.get", id=pid)
        p = (res or {}).get("pipeline") if isinstance(res, dict) else None
        branch = branch or (p or {}).get("branch") or ""
    if run is None and p is None:
        return {"error": f"run not found: {ref or pipeline}"}
    lane = next(iter(cv.lanes_of([r for r in rows if r.get("branch") == branch])), None) if branch else None
    items = []
    bres = await _read("board.items")
    for it in ((bres or {}).get("items") or []) if isinstance(bres, dict) else []:
        if (pid and it.get("pipeline") == pid) or (branch and it.get("branch") == branch):
            items.append(it)
    sid = (run or {}).get("session_id") or (p or {}).get("session_id") or ""
    return {"kind": cv.KIND, "view": "run", "title": branch or pid,
            "run": run, "cell": cv.cell_of(run) if run else None,
            "track": cv.track(p) if p else None, "lane": lane,
            "board_items": items,
            "conversation": {"session_id": sid,
                             "controller": cv.controller_of((run or {}).get("controller"),
                                                            (p or {}).get("controller"),
                                                            (p or {}).get("via"))} if sid else None}


# ─────────────────────────────────────────────────────────────────────────────
# loop.ci.* — the agentic loop
# ─────────────────────────────────────────────────────────────────────────────

async def _trace(session_id: str) -> Dict[str, Any]:
    res = await _read("workshop.agent_loop.trace", session_id=session_id)
    return res if isinstance(res, dict) else {}


async def _latest_session() -> str:
    ss = await _loop_sessions(5)
    return str((ss[0] if ss else {}).get("session_id") or "")


@capability(
    "loop.ci.matrix", memory="off", silent=True,
    http_method="GET", http_path="/loop/ci/matrix", http_tags=["ci", "workshop"],
    description=("The AGENTIC LOOP as a status matrix. With session_id: one lane per plan step, one "
                 "cell per tool call (ok/failed, ms, repeated). Without: one lane per goal, one cell "
                 "per loop run (last 100), so repeated goals race each other. Drawn as the "
                 "status-matrix widget. Output: {kind:'ci', view:'matrix', lanes, summary}."))
async def loop_ci_matrix(session_id: str = "", trace_id=None):
    if not session_id:
        return cv.loops_matrix(await _loop_sessions(100))
    t = await _trace(session_id)
    if t.get("error"):
        return t
    return cv.loop_matrix(t)


@capability(
    "loop.ci.race", memory="off", silent=True,
    http_method="GET", http_path="/loop/ci/race", http_tags=["ci", "workshop"],
    description=("One agentic loop's RACE TO GREEN: the completion gate's rounds and each step's "
                 "calls, red→green. Defaults to the newest loop. Drawn as the race-green widget. "
                 "Input: session_id. Output: {kind:'ci', view:'race', lanes, summary}."))
async def loop_ci_race(session_id: str = "", trace_id=None):
    session_id = session_id or await _latest_session()
    if not session_id:
        return {"error": "no loop sessions"}
    t = await _trace(session_id)
    if t.get("error"):
        return t
    return cv.loop_race(t)


@capability(
    "loop.ci.board", memory="off", silent=True,
    http_method="GET", http_path="/loop/ci/board", http_tags=["ci", "workshop"],
    description=("One agentic loop's PLAN as a board — planned · running · done · failed — each "
                 "step with its call count and time. Defaults to the newest loop. Drawn as the "
                 "ci-board widget. Input: session_id. Output: {kind:'ci', view:'board', columns, "
                 "summary}."))
async def loop_ci_board(session_id: str = "", trace_id=None):
    session_id = session_id or await _latest_session()
    if not session_id:
        return {"error": "no loop sessions"}
    t = await _trace(session_id)
    if t.get("error"):
        return t
    return cv.loop_board(t)



# ─────────────────────────────────────────────────────────────────────────────
# ci.branch — everything behind one branch (the sandbox menu's one call)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ci.branch", memory="off", silent=True,
    http_method="GET", http_path="/ci/branch", http_tags=["ci", "evolve"],
    description=("EVERYTHING BEHIND ONE BRANCH — the sandbox menu's one call. The branch's own "
                 "pipelines (every one kept), its gate lane and race to green, EVERY branch merged "
                 "into it (Loop Lab merges with their pipeline, controller, session and gate; plain "
                 "git merges too), the CONVERSATIONS that drove it and its merged branches (Claude / "
                 "Codex sessions joined from the pipelines, with title, turns and time span), and the "
                 "board items linked to any of them WITH their body and comment thread. Inputs: "
                 "branch (str!), repo (str=vera), limit (int=2000 merge commits to read; the answer "
                 "says `more` when history went further). Output: {kind:'ci', view:'branch', branch, "
                 "head, pipelines, lane, merged, conversations, board_items, summary}."))
async def ci_branch(branch: str = "", repo: str = "vera", limit: int = 2000, trace_id=None):
    branch = (branch or "").strip()
    if not branch:
        return {"error": "branch required"}
    ev = _ev()
    if ev is None:
        return {"error": "evolve not loaded"}
    root = await ev._resolve_repo_root(repo or "vera")
    if not (await ev._git("rev-parse", "--verify", f"refs/heads/{branch}", repo_root=root))["ok"]:
        return {"error": f"unknown branch: {branch}"}
    limit = max(1, min(20000, int(limit or 2000)))
    head = await ev._git("log", "-1", "--format=%H%x1f%cI%x1f%an%x1f%s", branch, repo_root=root)
    hp = (head.get("out") or "").split("\x1f")
    lg = await ev._git("log", "--merges", f"-n{limit + 1}", "--format=%H%x1f%cI%x1f%an%x1f%s",
                       branch, repo_root=root, timeout=120)
    lines = (lg.get("out") or "").splitlines()
    more = len(lines) > limit
    merged = [m for m in cv.parse_merges("\n".join(lines[:limit])) if m["branch"] != branch]

    pipes = await _pipelines()
    by_id = {str(p.get("id")): p for p in pipes}
    by_branch: Dict[str, List[Dict[str, Any]]] = {}
    for p in pipes:
        by_branch.setdefault(str(p.get("branch") or ""), []).append(p)
    for m in merged:
        p = by_id.get(m["pipeline_id"]) or (by_branch.get(m["branch"]) or [None])[0] or {}
        m.update({"pipeline_id": m["pipeline_id"] or str(p.get("id") or ""),
                  "controller": cv.controller_of(p.get("controller"), p.get("via")),
                  "session_id": str(p.get("session_id") or ""),
                  "gate": ("pass" if p.get("gate_passed") is True else
                           "fail" if p.get("gate_passed") is False else ""),
                  "decision": str(p.get("decision") or "")})
    mine = by_branch.get(branch, [])

    # conversations: every session that drove this branch or one merged into it
    convs: Dict[str, Dict[str, Any]] = {}

    def _conv(sid: str, ctl: str, br: str, pid: str, ts: str) -> None:
        if not sid:
            return
        c = convs.setdefault(sid, {"session_id": sid, "controller": ctl, "branches": [],
                                   "pipelines": [], "first_ts": ts, "last_ts": ts})
        if br and br not in c["branches"]:
            c["branches"].append(br)
        if pid and pid not in c["pipelines"]:
            c["pipelines"].append(pid)
        c["controller"] = c["controller"] or ctl
        if ts:
            c["first_ts"] = min(c["first_ts"] or ts, ts)
            c["last_ts"] = max(c["last_ts"] or ts, ts)
    for p in mine:
        _conv(str(p.get("session_id") or ""), cv.controller_of(p.get("controller"), p.get("via")),
              branch, str(p.get("id") or ""), str(p.get("created_at") or ""))
    for m in merged:
        _conv(m["session_id"], m["controller"], m["branch"], m["pipeline_id"], m["ts"])
    if convs:
        ls = await _read("ide.claude_sessions.list_sessions", max_sessions=500)
        known = {str(s.get("claude_session_id") or ""): s
                 for s in (((ls or {}).get("sessions") or []) if isinstance(ls, dict) else [])}
        for sid, c in convs.items():
            s = known.get(sid)
            if s:
                c.update({"title": s.get("title") or "", "turns": s.get("turns"),
                          "agent": s.get("agent") or "", "preview": s.get("last_preview") or "",
                          "session_first_ts": s.get("first_ts"), "session_last_ts": s.get("last_ts"),
                          "ingested": True})
            else:
                c["ingested"] = False

    # board items linked to the branch, a merged branch, or any of their pipelines
    brs = {branch} | {m["branch"] for m in merged}
    pids = {str(p.get("id")) for p in mine} | {m["pipeline_id"] for m in merged if m["pipeline_id"]}
    bres = await _read("board.items")
    linked = [it for it in (((bres or {}).get("items") or []) if isinstance(bres, dict) else [])
              if (it.get("branch") and it.get("branch") in brs)
              or (it.get("pipeline") and it.get("pipeline") in pids)]
    full: List[Dict[str, Any]] = []
    if linked:
        got = await asyncio.gather(*[_read("board.item.get", id=it.get("id")) for it in linked],
                                   return_exceptions=True)
        for it, g in zip(linked, got):
            item = (g or {}).get("item") if isinstance(g, dict) else None
            full.append(item or it)

    rows = [r for r in _attribute(await _history(), pipes) if r.get("branch") == branch]
    lane = next(iter(cv.lanes_of(rows)), None)
    merged.sort(key=lambda m: m["ts"], reverse=True)
    return {"kind": cv.KIND, "view": "branch", "title": branch, "branch": branch, "repo": repo or "vera",
            "head": {"sha": hp[0][:10] if hp and hp[0] else "", "ts": hp[1] if len(hp) > 1 else "",
                     "author": hp[2] if len(hp) > 2 else "", "subject": hp[3] if len(hp) > 3 else ""},
            "pipelines": mine, "lane": lane, "merged": merged, "more": more,
            "conversations": sorted(convs.values(), key=lambda c: c.get("last_ts") or "", reverse=True),
            "board_items": full,
            "summary": {"pipelines": len(mine), "merged": len(merged), "conversations": len(convs),
                        "board_items": len(full), "runs": len(rows),
                        "state": (lane or {}).get("state", "none")}}


# ─────────────────────────────────────────────────────────────────────────────
# ci.census — the census beside the commits that landed between its runs
# ─────────────────────────────────────────────────────────────────────────────

_CENSUS = TTLCache(60.0, max_entries=8)


@capability(
    "ci.census", memory="off", silent=True,
    http_method="GET", http_path="/ci/census", http_tags=["ci", "census"],
    description=("CENSUS x COMMITS: every trusted census run of a template, oldest to newest - goals done, capped, "
                 "wall time, quality - each beside the commits that LANDED on main before it (census.landed), with "
                 "its change against the run before judged against the measured noise floor (one capped goal, 13% "
                 "wall): `signal` or `noise`, so a better run is only called an improvement when it is one; plus "
                 "the goal x run matrix (each goal's outcome in each run). Drawn as the census-commits widget. "
                 "Inputs: template (str=default). Output: {kind:'ci', view:'census', runs:[{id, ended_at, done, "
                 "goals, capped, wall_s, quality, delta_done, delta_wall, verdict, direction, commits[], "
                 "commit_count}], goals:[{id, name, cells[]}], summary}."))
async def ci_census(template: str = "default", trace_id=None):
    template = (template or "default").strip()

    async def build():
        runs_r, landed_r, ov = await asyncio.gather(_read("census.runs"), _read("census.landed"),
                                                    _read("evolve.tasks.overview"))
        runs = list((runs_r or {}).get("runs") or []) if isinstance(runs_r, dict) else []
        prefix = "census-%s-" % template
        ids = [str(t.get("task_id")) for t in (((ov or {}).get("tasks") or []) if isinstance(ov, dict) else [])
               if str(t.get("task_id") or "").startswith(prefix)]
        # a template's own goals only: "census-default-style-broad-x" belongs to another template
        names = {x[len(prefix):] for x in ids}
        ids = [x for x in ids if not any(x[len(prefix):].startswith(n + "-") or x[len(prefix):].startswith("style-")
                                         for n in ("style",))]
        sem = asyncio.Semaphore(4)

        async def hist(tid):
            async with sem:
                h = await _read("evolve.task.history", id=tid)
            return tid, list((h or {}).get("results") or []) if isinstance(h, dict) else []
        goal_results = dict(await asyncio.gather(*[hist(t) for t in ids[:40]]))
        v = cv.census_view(runs, landed_r if isinstance(landed_r, dict) else {}, goal_results, template=template)
        v["summary"]["goal_ids"] = len(names)
        return v
    return await _CENSUS.get(("census", template), build)
