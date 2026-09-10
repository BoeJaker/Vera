"""census.* — the loop census as a first-class, queryable history.

The census harness (`run_census.py`) lives outside the repo and writes one
JSONL file per run. That was fine while a human read it with `tail`, and it is
why run 10's anomalies went unexplained for a pass: nothing put two runs side
by side, and nothing connected a run to the work it caused.

These capabilities close that. `census.runs` lists the history with a trend,
`census.run` opens one, `census.compare` puts two side by side per goal, and
`census.board` maps board items to the run that FOUND them and the run that
VERIFIED the fix — so a run reads as "these fixes landed, this is what is still
open" rather than a wall of counters.

READ-ONLY BY DESIGN. Nothing here starts, stops or edits a census run. The
harness owns the runs; this owns reading them. That matters because the census
is the measuring instrument — a UI that could quietly perturb it would make
every number it displays suspect.

The one deliberate exception is `census.control.set` (2026-09-10): it writes a
REQUEST — pause, resume or drop — that the harness polls and acts on itself,
so a prod restart no longer costs a census. It never touches a run file. See
`control.py` for the contract and why the default on restart is resume.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability
from Vera.vera.census import census_core as cc
from Vera.vera.census import operator_census_core as occ

import logging

log = logging.getLogger("vera.census")

# The harness is not part of the repo, so its location is configuration, not a
# constant. Default matches where it actually lives on prod.
CENSUS_DIR = Path(os.getenv("VERA_CENSUS_DIR", "")
                  or (Path.home() / "loop-census")).expanduser()

# A census file is small (a dozen records), but a mis-set VERA_CENSUS_DIR could
# point at anything, so cap what we will read rather than trusting the name.
_MAX_BYTES = 8 * 1024 * 1024
_MAX_RECORDS = 500

#: The per-goal ceiling the harness cancels at (run_census.py WALL_CAP_S). Read
#: here so the UI can say "this goal hit the wall" rather than hardcode 1800 in
#: JavaScript and quietly disagree with the runner if it ever changes.
CENSUS_WALL_CAP_S = int(os.getenv("VERA_CENSUS_WALL_CAP_S", "1800") or 1800)


# Parsed runs, keyed by (path, mtime_ns, size). An ARCHIVED run never changes,
# so re-parsing every file on every panel refresh is pure waste; only the live
# census.jsonl moves, and its stat changes when it does. Bounded, and evicted
# one entry at a time: it used to hold 64 and CLEAR itself when full, and the
# archive passed 64 files on 2026-09-10 - so every panel poll re-parsed every
# run, which is what "the census is very slow to update" was.
_CACHE: Dict[str, Tuple[Tuple[int, int], List[Dict[str, Any]]]] = {}
_CACHE_MAX = 512


def _stat_key(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _read_run_sync(path: Path) -> List[Dict[str, Any]]:
    """Parse one census JSONL file. A malformed line is skipped, not fatal —
    the file is appended to by a live run and can be caught mid-write."""
    key = _stat_key(path)
    cached = _CACHE.get(str(path))
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    out: List[Dict[str, Any]] = []
    try:
        if path.stat().st_size > _MAX_BYTES:
            log.warning("census: %s exceeds %d bytes — skipped", path.name, _MAX_BYTES)
            return out
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
                if len(out) >= _MAX_RECORDS:
                    break
    except FileNotFoundError:
        return out
    except Exception as e:
        log.warning("census: reading %s failed: %s", path, e)
        return out
    if key is not None:
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)   # oldest insertion first
        _CACHE[str(path)] = (key, out)
    return out


async def _read_run(path: Path) -> List[Dict[str, Any]]:
    """Off the event loop. These are small files on local disk, but the panel
    reads every run at once and the box is often mid-census — and synchronous
    I/O inside an async handler is exactly the stall pattern that produced the
    WebSocket flapping this codebase already has a fix for. Cheap to do right."""
    return await asyncio.to_thread(_read_run_sync, path)


def _redis():
    """The shared client, fetched late — importing it at module scope binds
    whatever REDIS was at import time, which is None during startup."""
    try:
        from Vera.vera import capability_orchestration as _orch
        return getattr(_orch, "REDIS", None)
    except Exception:
        return None


# The loop's own replay log. Same key the trace cap reads; the run's OUTPUT text
# only exists here (the run hash holds goal/status/timestamps and nothing else).
_EVENTS_KEY = "vera:loop:events:%s"
_MAX_EVENTS = 4000


async def _loop_events(session_id: str) -> List[Dict[str, Any]]:
    """Raw events for one loop run, or [] when they have aged out."""
    r = _redis()
    sid = (session_id or "").strip()
    if not r or not sid:
        return []
    try:
        raw = await r.lrange(_EVENTS_KEY % sid, 0, _MAX_EVENTS - 1)
    except Exception as e:
        log.info("census: events for %s unavailable: %s", sid, e)
        return []
    out: List[Dict[str, Any]] = []
    for x in raw or []:
        try:
            d = json.loads(x.decode() if isinstance(x, (bytes, bytearray)) else x)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


try:
    from Vera.vera.census import landed as _landed
    from Vera.vera.census import control as _ctl
except ImportError:                                   # pragma: no cover
    from vera.census import landed as _landed
    from vera.census import control as _ctl


def _run_files() -> Dict[str, Path]:
    """run_id -> file, for every census JSONL in the census dir."""
    found: Dict[str, Path] = {}
    try:
        for p in sorted(CENSUS_DIR.glob("census*.jsonl")):
            rid = cc.run_id_from_filename(p.name)
            if rid:
                found[rid] = p
    except Exception as e:
        log.warning("census: listing %s failed: %s", CENSUS_DIR, e)
    return found


# The operator census keeps its own files alongside the loop's, distinguished by
# prefix rather than directory so both are archived and rotated the same way.
_OP_PREFIX = "operator-census"


def _op_run_files() -> Dict[str, Path]:
    """run_id -> file, for every OPERATOR census JSONL."""
    found: Dict[str, Path] = {}
    try:
        for p in sorted(CENSUS_DIR.glob(_OP_PREFIX + "*.jsonl")):
            name = p.name[len(_OP_PREFIX):]
            # Reuse the loop census's naming rule by normalising the prefix:
            # "operator-census.run3.jsonl" -> "run3", bare -> "current".
            rid = cc.run_id_from_filename("census" + name)
            if rid:
                found[rid] = p
    except Exception as e:
        log.warning("census: listing operator runs in %s failed: %s", CENSUS_DIR, e)
    return found


@capability(
    "census.runs", memory="off", silent=True,
    http_method="GET", http_path="/census/runs", http_tags=["census", "workshop"],
    description=(
        "LIST every agentic-loop census run with its headline numbers and the "
        "coarse done-count trend. Each run: goals, done, wall_capped, "
        "unaccounted_total, gate_inserted_total, warnings_total, wall_total_s, "
        "and `counters_reconcile` — false when a run's own step accounting does "
        "not add up, meaning a number drawn from it may be measuring the "
        "instrument rather than the loop. These are counts, NOT a verdict on "
        "whether goals were achieved; use census.goal to judge a goal by its "
        "progression and output. "
        "PARTIAL AND FAILED RUNS ARE LEFT OUT BY DEFAULT — a run covering fewer "
        "goals than a full pass, or whose filename marks it partial/failed/"
        "stalled/aborted/interrupted/invalid/wedged, is not usable history and "
        "would sit in a trend as an ordinary point. Pass include_partial=true "
        "to get every run, each carrying `excluded` and `exclude_reason`. "
        "Inputs: include_partial (bool=false). Output: {runs[], count, "
        "shown_count, excluded_count, short_count, named_bad_count, "
        "full_goal_count, trend_complete, trend_trusted, dir}."),
)
async def cap_census_runs(include_partial: bool = False, trace_id=None) -> Dict[str, Any]:
    files = _run_files()
    # Concurrently, not one after another: this is the panel's first call and it
    # touches every run, so serialising the reads is the whole latency.
    records = await asyncio.gather(*(_read_run(p) for p in files.values()))
    summaries = [cc.summarise_run(rid, recs) for rid, recs in zip(files, records)]
    for s, recs in zip(summaries, records):
        # Which node and model served the run, and whether that changed mid-run
        # (two instruments in one file). Rows from before routing was recorded
        # report recorded_goals=0 rather than zeros that look like an answer.
        s["routing"] = _ctl.routing_rollup(recs)
        s["reruns"] = sum(len(r.get("reruns") or []) for r in recs if isinstance(r, dict))
        try:
            # When the run ended: the harness's .log beside the archive is
            # written as the run goes and never rewritten; the .jsonl is
            # rewritten by the backfill tools (code_version, reroutes) and its
            # mtime is whenever that last happened. Rows carry ended_at
            # themselves since 2026-09-10; the newest such row wins outright.
            p = files[s["run_id"]]
            lg = p.with_suffix(".log")
            ended = max((str(r.get("ended_at") or "") for r in recs if isinstance(r, dict)), default="")
            if ended:
                import calendar as _cal
                s["ended_at"] = float(_cal.timegm(time.strptime(ended[:19], "%Y-%m-%dT%H:%M:%S")))
            else:
                s["ended_at"] = (lg.stat() if lg.exists() else p.stat()).st_mtime
        except Exception:
            s["ended_at"] = None
    hist = cc.history(summaries)
    # The counts and both trends are computed over EVERY run and stay as they
    # were, so filtering the rows can never move a number. Only the rows the
    # caller reads are dropped, and `excluded_count` says how many, so a
    # shrunken list is never mistaken for a shrunken archive.
    rows = hist.get("runs") or []
    show_all = cc.truthy(include_partial)
    if not show_all:
        hist["runs"] = [r for r in rows if not r.get("excluded")]
    hist["shown_count"] = len(hist.get("runs") or [])
    hist["include_partial"] = show_all
    hist["dir"] = str(CENSUS_DIR)
    return hist


@capability(
    "census.run", memory="off", silent=True,
    http_method="GET", http_path="/census/run", http_tags=["census", "workshop"],
    description=(
        "ONE census run in full — its per-goal records (status, wall_s, planned/"
        "executed/inserted, gate_inserted, fast_path, unaccounted, cycles, "
        "warnings) plus the run summary. Inputs: run (str! — e.g. 'run11', or "
        "'current' for the live/most-recent file). Output: {run_id, summary, "
        "records[]}."),
)
async def cap_census_run(run: str = "", trace_id=None) -> Dict[str, Any]:
    rid = (run or cc.CURRENT).strip()
    files = _run_files()
    if rid not in files:
        return {"error": f"unknown run '{rid}'", "available": sorted(files)}
    records = await _read_run(files[rid])
    summary = cc.summarise_run(rid, records)
    summary["routing"] = _ctl.routing_rollup(records)
    boundary = {c.get("goal"): c for c in (summary.get("code") or {}).get("changes") or []}
    for r in records:
        if isinstance(r, dict):
            r["routing_summary"] = _ctl.routing_of(r)
            r["code_summary"] = cc.code_of(r)
            # The goal at which the code changed, so a row can be marked as the
            # boundary between two instruments without re-deriving it.
            if r.get("id") in boundary:
                r["code_summary"]["boundary"] = boundary[r.get("id")]
    return {"run_id": rid, "summary": summary, "records": records}


@capability(
    "census.compare", memory="off", silent=True,
    http_method="GET", http_path="/census/compare", http_tags=["census", "workshop"],
    description=(
        "Compare two census runs PER GOAL — `outcome_change` of improved / held "
        "/ regressed / missing, with wall-time and step deltas, worst news "
        "first. Always per-goal and never averaged: the goals differ by two "
        "orders of magnitude in cost, so a run-level mean says nothing. "
        "`outcome_change` is a status transition worth INVESTIGATING, not a "
        "score — a goal can reach 'done' having dropped half the request, and "
        "one still at 'wall-cap' can be doing better work than last time. Open "
        "census.goal on anything this flags. Inputs: base (str!), head (str! — "
        "e.g. base='run11' head='current'). Output: {base, head, goals[], "
        "counts, base_trusted, head_trusted}."),
)
async def cap_census_compare(base: str = "", head: str = "", trace_id=None) -> Dict[str, Any]:
    files = _run_files()
    b, h = (base or "").strip(), (head or cc.CURRENT).strip()
    missing = [x for x in (b, h) if x not in files]
    if missing:
        return {"error": f"unknown run(s): {', '.join(missing)}",
                "available": sorted(files)}
    br, hr = await asyncio.gather(_read_run(files[b]), _read_run(files[h]))
    goals = cc.compare_runs(br, hr)
    counts: Dict[str, int] = {}
    for g in goals:
        counts[g["outcome_change"]] = counts.get(g["outcome_change"], 0) + 1
    return {"base": b, "head": h, "goals": goals, "counts": counts,
            "base_trusted": cc.summarise_run(b, br)["counters_reconcile"],
            "head_trusted": cc.summarise_run(h, hr)["counters_reconcile"]}


_SESSIONS_KEY = "vera:loop:sessions"
_RUN_KEY = "vera:loop:run:%s"


def _rd(v: Any) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _is_stale(r, sid: str, run: Dict[str, Any]) -> bool:
    """Defer to the loop's own staleness rule — do not re-invent it.

    A run orphaned by a restart keeps `status: running` forever, because nothing
    is left alive to write a terminal status. The sessions route already corrects
    for this (`running` + stale → `interrupted`) via `_loop_run_is_stale`, which
    checks the in-process task registry first and only then falls back to the
    activity marker. Reading Redis directly and skipping that is how census.live
    came to report an 11-hour-old cancelled chat session as the live census run.

    If the helper cannot be imported, fail CLOSED — treat the run as stale rather
    than show a zombie as active. A missing live view is a gap; a confidently
    wrong one sends you debugging the wrong run.
    """
    try:
        from Vera.vera.dag.dag_workshop_capabilities import _loop_run_is_stale
    except Exception as e:                            # pragma: no cover
        log.info("census.live: staleness helper unavailable (%s) — not claiming live", e)
        return True
    try:
        return bool(await _loop_run_is_stale(r, sid, run))
    except Exception:
        return True


async def _running_loop(named_sid: str = "") -> Dict[str, Any]:
    """The loop session GENUINELY running, if any.

    /workshop/agent_loop/sessions is a plain route rather than a capability, so
    this reads the same Redis keys it does — including the same durable-history
    index with the resume index as fallback — instead of reaching over HTTP to
    our own process.

    `named_sid` is the session the harness says it is running (census.active
    .json). While the harness is live that name is the fact: the loop's own
    staleness rule hesitates during a long generation (no event for a while),
    and on 2026-09-10 that blink read as "no goal in flight" to a helper that
    then restarted prod mid-goal and cost a 17-minute run. A named session
    that still says running is returned without the staleness test.
    """
    r = _redis()
    if not r:
        return {}
    if named_sid:
        try:
            raw_run = await r.hgetall(_RUN_KEY % named_sid)
            if not raw_run:
                raw_run = await r.hgetall("vera:loop:history:run:%s" % named_sid)
            run = {_rd(k): _rd(v) for k, v in (raw_run or {}).items()}
            if run and run.get("status") == "running":
                run["session_id"] = named_sid
                run["named_by_harness"] = True
                return run
        except Exception as e:
            log.info("census.live: the harness's session %s unreadable: %s", named_sid[:12], e)
    sids: List[Any] = []
    try:
        sids = await r.zrevrange("vera:loop:history:index", 0, 40) or []
        if not sids:
            sids = await r.zrevrange(_SESSIONS_KEY, 0, 40) or []
    except Exception as e:
        log.info("census.live: session index unavailable: %s", e)
        return {}
    for raw in sids:
        sid = _rd(raw)
        try:
            raw_run = await r.hgetall(_RUN_KEY % sid)
            if not raw_run:
                raw_run = await r.hgetall("vera:loop:history:run:%s" % sid)
            run = {_rd(k): _rd(v) for k, v in (raw_run or {}).items()}
        except Exception:
            continue
        if not run or run.get("status") != "running":
            continue
        if await _is_stale(r, sid, run):
            continue          # orphaned by a restart, not actually running
        run["session_id"] = sid
        return run
    return {}


def _goal_ids_sync() -> List[str]:
    try:
        with (CENSUS_DIR / "goals.json").open(encoding="utf-8") as fh:
            return [str(g.get("id")) for g in json.load(fh) if isinstance(g, dict)]
    except Exception:
        return []


@capability(
    "census.live", memory="off", silent=True,
    http_method="GET", http_path="/census/live", http_tags=["census", "workshop"],
    description=(
        "The census RUN IN FLIGHT — what it has finished, what it is working on "
        "right now, and that goal's live stats. A goal only lands in "
        "census.jsonl when it FINISHES, so for the one-to-three hours a census "
        "takes the history shows nothing; this is that window. Returns "
        "goals_total/completed/remaining from goals.json (real queue order), "
        "plus the running loop's goal, elapsed seconds, live counters and its "
        "steps so far with cycle counts. Safe to poll. Output: {active, "
        "progress, recent[], counters, steps[]}."),
)
async def cap_census_live(trace_id=None) -> Dict[str, Any]:
    files = _run_files()
    done = await _read_run(files[cc.CURRENT]) if cc.CURRENT in files else []
    # The harness says exactly which template and goal it is on; goals.json is
    # the fallback for a harness from before it wrote the active file.
    harness = await asyncio.to_thread(_read_active_sync)
    control = await asyncio.to_thread(_read_control_sync)
    goal_ids = list(harness.get("goal_ids") or []) if harness.get("live") else []
    if not goal_ids:
        goal_ids = await asyncio.to_thread(_goal_ids_sync)
    run = await _running_loop(str(harness.get("session_id") or "") if harness.get("live") else "")

    counters: Dict[str, Any] = {}
    steps: List[Dict[str, Any]] = []
    active_goal = ""
    sid = str(run.get("session_id") or "")
    if sid:
        try:
            fn = (CAPABILITY_REGISTRY.get("workshop.agent_loop.trace") or {}).get("func")
            if fn is not None:
                tr = await fn(session_id=sid) or {}
                counters = tr.get("counters") or {}
                for s in tr.get("steps") or []:
                    steps.append({"id": s.get("step_id"), "title": s.get("title") or "",
                                  "ok": s.get("ok"), "cycles": len(s.get("calls") or [])})
        except Exception as e:
            log.info("census.live: trace for %s unavailable: %s", sid, e)
    # The harness names the goal in flight; before it did, the running loop was
    # matched back to a goal by its goal text against goals.json.
    gtext = str(run.get("goal") or "")
    if harness.get("live") and harness.get("current_goal") and (
            not sid or harness.get("session_id") in ("", sid)):
        active_goal = str(harness.get("current_goal"))
    elif gtext:
        try:
            with (CENSUS_DIR / "goals.json").open(encoding="utf-8") as fh:
                for g in json.load(fh):
                    if isinstance(g, dict) and str(g.get("goal") or "")[:80] == gtext[:80]:
                        active_goal = str(g.get("id")); break
        except Exception:
            pass

    elapsed = None
    try:
        from datetime import datetime, timezone
        st = str(run.get("started_at") or "").replace("Z", "+00:00")
        if st:
            elapsed = round((datetime.now(timezone.utc)
                             - datetime.fromisoformat(st)).total_seconds(), 1)
    except Exception:
        pass

    # The goal's own LLM calls so far, for the live dash: from the in-process
    # request log, since the loop started (the census holds the box alone).
    routing: Dict[str, Any] = {}
    if sid:
        try:
            fn = (CAPABILITY_REGISTRY.get("ollama.request_log") or {}).get("func")
            if fn is not None:
                d = await fn(limit=600) or {}
                routing = _ctl.live_routing(d.get("entries") or [], str(run.get("started_at") or ""),
                                            session_id=sid)
        except Exception as e:
            log.info("census.live: request log unavailable: %s", e)

    return {
        "active": ({"session_id": sid, "goal": gtext, "goal_id": active_goal,
                    "started_at": run.get("started_at"), "elapsed_s": elapsed}
                   if sid else None),
        "progress": cc.live_progress(goal_ids, done, active_goal),
        "counters": counters,
        "steps": steps,
        "routing": routing,
        # Every goal this run has finished, not the last six: a 12-goal census
        # was showing half its own progress, which is why the only way to see
        # how a run was going was to load it into the Compare table.
        "recent": [{"id": r.get("id"), "status": r.get("status"),
                    "wall_s": r.get("wall_s"), "wall_cap_s": r.get("wall_cap_s"),
                    "quality": _q_brief(r.get("quality")),
                    "routing": _ctl.routing_of(r),
                    "code": cc.code_of(r),
                    "reruns": len(r.get("reruns") or [])} for r in done],
        # The ceiling the harness cancels at. Returned so the UI can mark a
        # goal that ran up against it instead of assuming a number — half the
        # default goal set finishes within a minute of the cap, so "did it hit
        # the wall" is the difference between a pass and a timeout.
        "wall_cap_s": CENSUS_WALL_CAP_S if not harness.get("wall_cap_s")
                      else int(harness["wall_cap_s"]),
        # What the harness itself reports (template, goal, paused, done/total,
        # liveness) and what it is currently being asked (run / pause / drop).
        "harness": harness,
        "control": {"state": _ctl.control_state(control), "reason": control.get("reason") or "",
                    "by": control.get("by") or "", "ts": control.get("ts") or ""},
    }


def _q_brief(q: Any) -> Dict[str, Any]:
    if not isinstance(q, dict):
        return {}
    return {"passed": q.get("passed"), "total": q.get("total"),
            "files_missing": q.get("files_missing") or []}


@capability(
    "census.goal", memory="off", silent=True,
    http_method="GET", http_path="/census/goal", http_tags=["census", "workshop"],
    description=(
        "DRILL INTO one goal of one run — the material needed to JUDGE it, not "
        "to score it. Returns what the run was asked to satisfy (done_when), "
        "the steps it actually chose with their caps, tools, cycles and "
        "failures, which steps the planner never listed, whether the completion "
        "gate still thought something was missing, and the run's warnings. "
        "STEP COUNTS SETTLE NOTHING on their own: fewer steps can mean a "
        "tighter plan or a plan that dropped half the request, and 'done' is a "
        "coarse harness outcome. Read the progression and the output. Inputs: "
        "run (str='current'), goal (str! — e.g. 'build-multifile'). Output: "
        "{run_id, evidence, record}."),
)
async def cap_census_goal(run: str = "", goal: str = "", trace_id=None) -> Dict[str, Any]:
    rid = (run or cc.CURRENT).strip()
    gid = (goal or "").strip()
    files = _run_files()
    if rid not in files:
        return {"error": f"unknown run '{rid}'", "available": sorted(files)}
    rec = next((r for r in await _read_run(files[rid]) if r.get("id") == gid), None)
    if rec is None:
        return {"error": f"goal '{gid}' not in run '{rid}'"}
    trace: Dict[str, Any] = {}
    sid = str(rec.get("session_id") or "")
    if sid:
        try:
            fn = (CAPABILITY_REGISTRY.get("workshop.agent_loop.trace") or {}).get("func")
            if fn is not None:
                trace = await fn(session_id=sid, include_text=True) or {}
        except Exception as e:
            # The event log has a retention window, so an old run's trace can be
            # gone. That is a missing view, not an error worth failing on.
            log.info("census.goal: trace for %s unavailable: %s", sid, e)
            trace = {"error": f"{type(e).__name__}: {e}"}
    # The OUTPUT — what the loop actually wrote. Neither the census record nor
    # the trace digest carries a line of it, and judging a run means reading it.
    events = await _loop_events(sid)
    return {"run_id": rid, "record": rec,
            "evidence": cc.goal_evidence(rec, trace),
            "outputs": cc.run_outputs(events),
            "events_available": len(events),
            "trace_available": bool(trace and not trace.get("error"))}


@capability(
    "census.operator.runs", memory="off", silent=True,
    http_method="GET", http_path="/census/operator/runs", http_tags=["census", "operator"],
    description=(
        "LIST operator-census runs — the browser's own measuring instrument. Per "
        "run: goals, and how many FINISHED (ran cleanly to a stop), ERRORED, hit "
        "their step CEILING, or are INCOMPLETE (no completion event at all), plus "
        "total steps/errors, how many goals showed the thrash signature, and "
        "`comparable` (false when any goal is incomplete, since an incomplete "
        "goal has no trustworthy duration or step count). NOTE: `finished` means "
        "ran cleanly to a stop, NOT that the browser achieved the goal — nothing "
        "here decides that. Output: {runs[], count, dir}."),
)
async def cap_census_operator_runs(trace_id=None) -> Dict[str, Any]:
    files = _op_run_files()
    records = await asyncio.gather(*(_read_run(p) for p in files.values()))
    runs = [occ.summarise_run(rid, recs) for rid, recs in zip(files, records)]
    runs.sort(key=lambda s: cc.run_sort_key(str(s.get("run_id") or "")))
    return {"runs": runs, "count": len(runs), "dir": str(CENSUS_DIR)}


@capability(
    "census.operator.run", memory="off", silent=True,
    http_method="GET", http_path="/census/operator/run", http_tags=["census", "operator"],
    description=(
        "ONE operator-census run in full — every goal's record, each carrying the "
        "`run_id` of the underlying browser run so a row can be opened straight "
        "into operator.trace for the step-by-step. Inputs: run (str='current'). "
        "Output: {run_id, summary, records[]}."),
)
async def cap_census_operator_run(run: str = "", trace_id=None) -> Dict[str, Any]:
    rid = (run or cc.CURRENT).strip()
    files = _op_run_files()
    if rid not in files:
        return {"error": f"unknown operator run '{rid}'", "available": sorted(files)}
    records = await _read_run(files[rid])
    return {"run_id": rid, "summary": occ.summarise_run(rid, records),
            "records": records}


@capability(
    "census.operator.compare", memory="off", silent=True,
    http_method="GET", http_path="/census/operator/compare", http_tags=["census", "operator"],
    description=(
        "Compare two operator-census runs PER GOAL, worst news first. The outcome "
        "ladder is worst-to-best incomplete < ceiling < errored < finished — a run "
        "at its CEILING ranks BELOW one that errored honestly, because its own "
        "`reason` may still read like success while it merely ran out of steps "
        "(the failure L8 was landed for). `outcome_change` is a transition to "
        "investigate, not a score. Inputs: base (str!), head (str!). Output: "
        "{base, head, goals[], counts}."),
)
async def cap_census_operator_compare(base: str = "", head: str = "",
                                      trace_id=None) -> Dict[str, Any]:
    files = _op_run_files()
    b, h = (base or "").strip(), (head or cc.CURRENT).strip()
    missing = [x for x in (b, h) if x not in files]
    if missing:
        return {"error": f"unknown run(s): {', '.join(missing)}",
                "available": sorted(files)}
    br, hr = await asyncio.gather(_read_run(files[b]), _read_run(files[h]))
    goals = occ.compare_runs(br, hr)
    counts: Dict[str, int] = {}
    for g in goals:
        counts[g["outcome_change"]] = counts.get(g["outcome_change"], 0) + 1
    return {"base": b, "head": h, "goals": goals, "counts": counts,
            "base_comparable": occ.summarise_run(b, br)["comparable"],
            "head_comparable": occ.summarise_run(h, hr)["comparable"]}


@capability(
    "census.board", memory="off", silent=True,
    http_method="GET", http_path="/census/board", http_tags=["census", "workshop"],
    description=(
        "Map board items to census runs — which run FOUND each issue and which "
        "run VERIFIED the fix, so a run reads as the work it caused rather than "
        "a wall of counters. Items declare this with labels "
        "`census:found:<run_id>` and `census:fixed:<run_id>` (labels, not new "
        "board fields: the board schema is shared with every other agent, and a "
        "convention costs nothing to adopt or abandon). Inputs: run (str — one "
        "run, else all). Output: {by_run:{<run>:{found[],fixed[]}}, "
        "found_prefix, fixed_prefix}."),
)
async def cap_census_board(run: str = "", trace_id=None) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    try:
        fn = (CAPABILITY_REGISTRY.get("board.items") or {}).get("func")
        if fn is None:
            return {"by_run": {}, "error": "board.items is not registered",
                    "found_prefix": cc.FOUND_PREFIX, "fixed_prefix": cc.FIXED_PREFIX}
        res = await fn()
        items = (res or {}).get("items") or []
    except Exception as e:
        # The board is a peer subsystem; if it is unreachable the census view
        # still works, just without its linkage. Say so rather than 500.
        log.warning("census.board: board.items unavailable: %s", e)
        return {"by_run": {}, "error": f"board unavailable: {type(e).__name__}: {e}",
                "found_prefix": cc.FOUND_PREFIX, "fixed_prefix": cc.FIXED_PREFIX}
    by_run = cc.board_links_by_run(items)
    rid = (run or "").strip()
    if rid:
        by_run = {rid: by_run.get(rid, {"found": [], "fixed": []})}
    return {"by_run": by_run, "found_prefix": cc.FOUND_PREFIX,
            "fixed_prefix": cc.FIXED_PREFIX, "items_scanned": len(items)}


@capability(
    "census.landed", memory="off", silent=True,
    http_method="GET", http_path="/census/landed", http_tags=["census", "workshop"],
    description=(
        "What actually LANDED between consecutive census runs, derived from the "
        "repository rather than from anybody remembering to label a board item "
        "- which is why the board-driven view stagnated. A commit is attributed "
        "to the first census that finished after it; the window is returned so "
        "a commit that landed mid-run is visible as such rather than presented "
        "as a tidy fact. Inputs: repo (str=vera), limit (int=300 commits). "
        "Output: {by_run:{run_id:{window_from,window_to,commits[],count,merges,"
        "summary}}, runs[], repo}."),
)
async def cap_census_landed(repo: str = "vera", limit: int = 300,
                            trace_id=None) -> Dict[str, Any]:
    import subprocess

    root = "/home/boejaker/Vera"
    try:
        out = subprocess.run(
            ["git", "-C", root, "log", "main", "-n", str(max(1, int(limit))),
             "--pretty=format:" + _landed.GIT_FORMAT],
            capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return {"error": "git log failed: %s" % e, "by_run": {}, "runs": []}

    commits = _landed.parse_log(out)
    runs = []
    records_by_run = await asyncio.gather(
        *(_read_run(p) for p in _run_files().values()))
    for (rid, path), recs in zip(_run_files().items(), records_by_run):
        try:
            ended = path.stat().st_mtime
        except Exception:
            continue
        # Start is derived, not stored: ended minus the wall time the goals
        # actually consumed. It underestimates (it ignores the gaps between
        # goals) and so flags fewer commits as mid-run, which is the safe
        # direction - see landed.assign.
        spent = 0.0
        for r in (recs or []):
            try:
                spent += float((r or {}).get("wall_s") or 0)
            except (TypeError, ValueError):
                pass
        runs.append({"run_id": rid, "ended_at": ended,
                     "started_at": (ended - spent) if spent else None})
    by_run = _landed.assign(commits, runs)
    for rid, entry in by_run.items():
        entry["summary"] = _landed.summarise(entry)
    return {"by_run": by_run,
            "runs": sorted((r["run_id"] for r in runs)),
            "repo": repo, "commits_scanned": len(commits)}


# ── control across restarts, and what the harness is doing right now ─────────

def _control_path() -> str:
    return str(CENSUS_DIR / _ctl.CONTROL_NAME)


def _active_path() -> str:
    return str(CENSUS_DIR / _ctl.ACTIVE_NAME)


def _read_control_sync() -> Dict[str, Any]:
    return _ctl.read_json(_control_path())


def _read_active_sync() -> Dict[str, Any]:
    return _ctl.active_view(_ctl.read_json(_active_path()))


async def census_control_view() -> Dict[str, Any]:
    control, active = await asyncio.gather(asyncio.to_thread(_read_control_sync),
                                           asyncio.to_thread(_read_active_sync))
    return {"control": control, "state": _ctl.control_state(control),
            "active": active, "dir": str(CENSUS_DIR)}


@capability(
    "census.control", memory="off", silent=True,
    http_method="GET", http_path="/census/control", http_tags=["census", "workshop"],
    description=(
        "The census control state and what the harness reports it is doing. "
        "`state` is run / pause / drop (what census.control.json currently asks "
        "of the harness); `active` is the harness's own report — template, goal "
        "in flight, goals done/total, running/paused/done/dropped — with `live` "
        "false when that report is stale (a harness that died without saying "
        "so). Safe to poll. Output: {control, state, active, dir}."),
)
async def cap_census_control(trace_id=None) -> Dict[str, Any]:
    return await census_control_view()


@capability(
    "census.control.set", memory="off",
    http_method="POST", http_path="/census/control/set", http_tags=["census", "workshop"],
    description=(
        "ASK the running census to pause, resume or drop. The harness polls the "
        "control file every 15 s and acts on it: pause cancels the goal in flight "
        "and, once resumed and prod is healthy, RE-RUNS that goal from scratch "
        "(the abandoned attempt is noted on the row, never recorded as a result); "
        "drop cancels the goal and ends the whole set, archived as -dropped. "
        "sys.dev.restart writes a pause itself (default) or a drop "
        "(resume_census=false) before it re-execs. Inputs: action (str! — pause|"
        "resume|drop), reason (str), by (str). Output: {ok, wrote, state, active}."),
)
async def cap_census_control_set(action: str = "", reason: str = "", by: str = "",
                                 trace_id=None) -> Dict[str, Any]:
    try:
        data = _ctl.make_control(action, reason=reason, by=by or "census.control.set")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ok = await asyncio.to_thread(_ctl.write_json, _control_path(), data)
    if not ok:
        return {"ok": False, "error": "could not write %s" % _control_path()}
    view = await census_control_view()
    log.warning("census.control: %s (%s) by %s", data.get("pause") and "PAUSE"
                or data.get("drop") and "DROP" or "RESUME", reason or "-", by or "-")
    return {"ok": True, "wrote": data, **view}


async def census_before_restart(resume: bool, by: str = "sys.dev.restart") -> Dict[str, Any]:
    """Called by sys.dev.restart before it re-execs. Writes a pause (to be lifted
    on the way back up) or a drop, but ONLY when a census is genuinely live —
    a pause left on file with nothing running would stop the next census cold."""
    active = await asyncio.to_thread(lambda: _ctl.read_json(_active_path()))
    control = await asyncio.to_thread(lambda: _ctl.read_json(_control_path()))
    plan = _ctl.restart_plan(active, resume, control)
    if plan["action"] == "none":
        if plan.get("kept"):
            log.warning("census: a person's pause is on file (%s) - the restart leaves it; "
                        "the harness stays paused after the restart", plan["why"])
        return plan
    data = _ctl.make_control(plan["action"], reason=_ctl.RESTART_REASON, by=by,
                             resume_on_start=(plan["action"] == "pause"))
    plan["wrote"] = await asyncio.to_thread(_ctl.write_json, _control_path(), data)
    plan["written_at"] = str(data.get("ts") or "")
    plan["ack_wait_max_s"] = _ctl.PAUSE_ACK_MAX_S
    log.warning("census: %s written before restart (%s)", plan["action"].upper(), plan["why"])
    return plan


async def census_wait_acked(plan: Dict[str, Any], max_wait_s: float = 0.0) -> Dict[str, Any]:
    """Wait (bounded) for the harness to act on what census_before_restart
    wrote, so the re-exec happens AFTER the loop is cancelled and the harness
    is parked, not under it. Returns {acked, waited_s}."""
    action = str((plan or {}).get("action") or "none")
    since = str((plan or {}).get("written_at") or "")
    if action == "none" or not (plan or {}).get("wrote"):
        return {"acked": False, "waited_s": 0.0, "why": "nothing written"}
    limit = float(max_wait_s or _ctl.PAUSE_ACK_MAX_S)
    t0 = time.time()
    while time.time() - t0 < limit:
        active = await asyncio.to_thread(lambda: _ctl.read_json(_active_path()))
        if _ctl.pause_acked(active, since, action):
            waited = round(time.time() - t0, 1)
            log.warning("census: harness acknowledged the %s after %ss", action, waited)
            return {"acked": True, "waited_s": waited}
        await asyncio.sleep(1.0)
    waited = round(time.time() - t0, 1)
    log.warning("census: harness did not acknowledge the %s within %ss; restarting anyway",
                action, waited)
    return {"acked": False, "waited_s": waited, "why": "timeout"}


def _lift_restart_pause_sync() -> Dict[str, Any]:
    """On startup: lift a pause that a restart wrote asking to resume. A pause a
    person wrote is left alone. Idempotent — the module body runs more than
    once per process and a second lift finds nothing to do."""
    path = _control_path()
    control = _ctl.read_json(path)
    if not _ctl.should_lift_on_start(control):
        return {"lifted": False, "state": _ctl.control_state(control)}
    data = _ctl.make_control("resume", reason="lifted on startup after restart",
                             by="census startup")
    ok = _ctl.write_json(path, data)
    return {"lifted": bool(ok), "state": "run" if ok else _ctl.control_state(control)}


try:
    _lift = _lift_restart_pause_sync()
    if _lift.get("lifted"):
        log.warning("census: restart pause LIFTED on startup — the harness will resume "
                    "and re-run the goal it was on")
except Exception as _e:                                  # pragma: no cover
    log.info("census: startup pause check skipped: %s", _e)

