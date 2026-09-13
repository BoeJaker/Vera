"""evolve.ship.branches - the Ship page's one table, one call.

CI/CD, Review, Sources, Sandbox and Unit tests were five Loop Lab pages that
each listed the same branches from a different store. This capability reads
those stores in one go (pipeline list, sandbox registry, unit-test history,
integration edges, git status, pending workspace proposals) and hands back
one row per branch (ship_core), plus the page-level facts the old pages'
headers carried: the primary runner, sandbox capacity, the edges, the
test race-to-green strip. Loaded after evolve/ and ide/ so it can reach
their readers through sys.modules (the loader registers modules by bare
filename).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import ship_core as sc
except ImportError:                                   # pragma: no cover
    from vera.evolve import ship_core as sc

log = logging.getLogger("vera.evolve.ship")

PIPELINES_N = 200     # the branches table reaches this far back into the pipeline list
TEST_RUNS_N = 300     # ...and this far into the unit-test history


def _mods():
    return sys.modules.get("evolve_capabilities"), sys.modules.get("ide_capabilities")


async def _safe(coro, default):
    try:
        return await coro
    except Exception as e:
        log.info("ship: a reader failed: %s", e)
        return default


async def _nothing():
    return {}


def _truthy(v: Any, default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@capability(
    "evolve.ship.branches", memory="off", silent=True,
    http_method="GET", http_path="/evolve/ship/branches", http_tags=["evolve"],
    description=(
        "Every BRANCH Loop Lab knows, in one shape: its latest pipeline (and how many "
        "are holding for a decision), its sandbox (running / paused / dirty / merged), "
        "its unit-test story (latest run, red runs, test-count delta), its role "
        "(main | edge | mirror | feature) and one STAGE word - live, review, red, "
        "merged, idle - live and awaiting-a-decision first. This is the Ship page's "
        "table; CI/CD, Review, Sandbox and Unit tests read from it. Filters: role, "
        "stage, text, hide_merged (bool=false), detail (bool=true - sandbox git/dirty "
        "probes; false is cheaper for a poll), limit (int=300). Output: {branches[], "
        "count, total, summary{stages,roles,sandboxes}, edges[], main, git{branch,dirty}, "
        "capacity, runner (evolve.sandbox.status), proposals[] (workspace changes awaiting "
        "review), tests{lanes,trend,race,regressions}, any_live}."),
)
async def cap_evolve_ship_branches(role: str = "", stage: str = "", text: str = "", hide_merged: Any = False,
                                   detail: Any = True, limit: int = 300, trace_id=None) -> Dict[str, Any]:
    ev, ide = _mods()
    if ev is None:
        return {"error": "evolve module unavailable", "branches": [], "count": 0, "total": 0}
    want_detail = _truthy(detail, True)
    pl, sl, uh, be, gs, st, ws = await asyncio.gather(
        _safe(ev.evolve_pipeline_list(limit=PIPELINES_N), {}),
        _safe(ev.evolve_sandbox_list(detail=want_detail), {}),
        _safe(ev.evolve_unittest_history(limit=TEST_RUNS_N), {}),
        _safe(ev.evolve_bleeding_edge_list(), {}),
        _safe(ev.evolve_git_status(), {}),
        _safe(ev.evolve_sandbox_status(), {}),
        _safe(ide.ide_ws_changes_list(status="pending"), {}) if ide is not None else _nothing(),
    )
    git = gs if isinstance(gs, dict) and not gs.get("error") else {}
    main = str((be or {}).get("main") or "main")
    rows = sc.branch_rows((pl or {}).get("pipelines") or [], (sl or {}).get("sandboxes") or [],
                          (uh or {}).get("runs") or [], (be or {}).get("edges") or [], git, main=main)
    shown = sc.filter_rows(rows, role=role or "", stage=stage or "", text=text or "",
                           hide_merged=_truthy(hide_merged, False))
    # Sandbox -> Workspace Changes proposals live in the shared changes store,
    # not the pipeline queue; the Review page surfaced the Loop Lab ones.
    proposals = [p for p in ((ws or {}).get("proposals") or [])
                 if str(p.get("source") or "").startswith("loop-lab:")
                 or str(p.get("workspace") or "").startswith("Loop Lab")]
    try:
        lim = max(1, int(limit))
    except Exception:
        lim = 300
    return {
        "branches": shown[:lim], "count": len(shown), "total": len(rows), "summary": sc.summary(rows),
        "edges": (be or {}).get("edges") or [], "edges_default": (be or {}).get("default") or "", "main": main,
        "git": {"branch": git.get("branch") or "", "dirty": bool(git.get("dirty")),
                "dirty_files": len(git.get("dirty_files") or []), "branches": git.get("branches") or []},
        "capacity": (sl or {}).get("capacity"), "runner": st or {}, "proposals": proposals,
        "tests": {"lanes": (uh or {}).get("lanes") or [], "trend": (uh or {}).get("trend"),
                  "race": (uh or {}).get("race"), "regressions": (uh or {}).get("regressions") or []},
        "any_live": any(r.get("live") for r in rows),
    }
