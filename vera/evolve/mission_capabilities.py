"""evolve.mission.events - Mission control's one table and its live strip,
one call.

Master, Activity and Errors were three Loop Lab pages over the same
happenings: the audit log, the errors work-queue, the gates, and a glance
at what needs a person. This capability reads them in one go and hands back
a row per event (mission_core) plus the strip above the table: what is
live now (evolve.work.live - the census, the suite, the improvement
session, the single run), the live pipelines, what needs a promote, the
active board items, the errors by state, the last gate, and the autonomous
lock. Loaded after evolve/ and task_history/ so it reaches their readers
through sys.modules (the loader registers modules by bare filename).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import mission_core as mc
except ImportError:                                   # pragma: no cover
    from vera.evolve import mission_core as mc

log = logging.getLogger("vera.evolve.mission")

AUDIT_N = 300
ERRORS_N = 200
GATES_N = 60
PIPELINES_N = 60


def _mods():
    return sys.modules.get("evolve_capabilities"), sys.modules.get("task_history_capabilities")


async def _safe(coro, default):
    try:
        out = await coro
    except Exception as e:
        log.info("mission: a reader failed: %s", e)
        return default
    if isinstance(out, dict) and out.get("error") and not out.get("ok"):
        log.info("mission: a reader answered an error: %s", out.get("error"))
        return default
    return out


async def _nothing():
    return {}


def _truthy(v: Any, default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@capability(
    "evolve.mission.events", memory="off", silent=True,
    http_method="GET", http_path="/evolve/mission/events", http_tags=["evolve"],
    description=(
        "Every EVENT Loop Lab knows, in one shape, newest first, open errors first: "
        "an audit ACTION (every promote, rollback, merge, sandbox exec, config change - "
        "who, ok, the record it names), an ERROR in the work-queue (state new | "
        "suggested | approved | applied | dismissed, its suggested fix), a GATE (a "
        "unit-test run: passed / failed / total, its pipeline). This is Mission "
        "control's table; Activity, Errors and Master read from it. With it the strip: "
        "live (evolve.work.live: census, suite, improvement session, single run), "
        "autonomous (the main lock), counts (needs_promotion, live_pipelines, "
        "active_items, errors by state, last gate). Filters: kind (action|error|gate), "
        "family (pipeline, sandbox, unittest, ...), text, who, problems (bool=false), "
        "hide_exec (bool=false - drop the sandbox.exec chatter), limit (int=300). "
        "Output: {events[], count, total, summary{kinds,families,problems}, families[], "
        "live, autonomous, counts, any_live}."),
)
async def cap_evolve_mission_events(kind: str = "", family: str = "", text: str = "", who: str = "",
                                    problems: Any = False, hide_exec: Any = False, limit: int = 300,
                                    trace_id=None) -> Dict[str, Any]:
    ev, th = _mods()
    if ev is None:
        return {"error": "evolve module unavailable", "events": [], "count": 0, "total": 0}
    audit, errors, tests, pl, bd, live, auto = await asyncio.gather(
        _safe(ev.evolve_audit_list(limit=AUDIT_N), {}),
        _safe(ev.evolve_errors_list(limit=ERRORS_N), {}),
        _safe(ev.evolve_unittest_history(limit=GATES_N), {}),
        _safe(ev.evolve_pipeline_list(limit=PIPELINES_N), {}),
        _safe(ev._call("board.items"), {}),
        _safe(th.cap_evolve_work_live(), {}) if th is not None else _nothing(),
        _safe(ev.autonomous_status(), {}),
    )
    audit_rows = (audit or {}).get("audit") or []
    error_items = (errors or {}).get("items") or []
    test_runs = (tests or {}).get("runs") or []
    pipelines = (pl or {}).get("pipelines") or []
    items = (bd or {}).get("items") or []
    rows = mc.event_rows(audit_rows, error_items, test_runs)
    shown = mc.filter_events(rows, kind=kind or "", family=family or "", text=text or "", who=who or "",
                             problems=_truthy(problems, False), hide_exec=_truthy(hide_exec, False))
    try:
        lim = max(1, int(limit))
    except Exception:
        lim = 300
    c = mc.counts(pipelines, items, error_items, test_runs)
    live = live if isinstance(live, dict) else {}
    any_live = bool(live.get("any_live") or c.get("live_pipelines"))
    return {
        "events": shown[:lim], "count": len(shown), "total": len(rows), "summary": mc.summary(rows),
        "families": sorted({r["family"] for r in rows if r.get("family")}),
        "live": live, "autonomous": auto if isinstance(auto, dict) else {}, "counts": c, "any_live": any_live,
        "errors_counts": (errors or {}).get("counts") or {},
    }
