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
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability
from Vera.vera.census import census_core as cc

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


def _read_run(path: Path) -> List[Dict[str, Any]]:
    """Parse one census JSONL file. A malformed line is skipped, not fatal —
    the file is appended to by a live run and can be caught mid-write."""
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
        pass
    except Exception as e:
        log.warning("census: reading %s failed: %s", path, e)
    return out


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
        "progression and output. Output: {runs[], count, trend_all, "
        "trend_trusted, dir}."),
)
async def cap_census_runs(trace_id=None) -> Dict[str, Any]:
    summaries = [cc.summarise_run(rid, _read_run(p))
                 for rid, p in _run_files().items()]
    hist = cc.history(summaries)
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
    records = _read_run(files[rid])
    return {"run_id": rid, "summary": cc.summarise_run(rid, records),
            "records": records}


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
    br, hr = _read_run(files[b]), _read_run(files[h])
    goals = cc.compare_runs(br, hr)
    counts: Dict[str, int] = {}
    for g in goals:
        counts[g["outcome_change"]] = counts.get(g["outcome_change"], 0) + 1
    return {"base": b, "head": h, "goals": goals, "counts": counts,
            "base_trusted": cc.summarise_run(b, br)["counters_reconcile"],
            "head_trusted": cc.summarise_run(h, hr)["counters_reconcile"]}


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
    rec = next((r for r in _read_run(files[rid]) if r.get("id") == gid), None)
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
    return {"run_id": rid, "record": rec,
            "evidence": cc.goal_evidence(rec, trace),
            "trace_available": bool(trace and not trace.get("error"))}


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
