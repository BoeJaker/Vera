"""evolve.results / evolve.task.history / evolve.tasks.overview — one task
through time, across every driver that ran it.

The census archive (JSONL, read by census_capabilities) and the suite's run
records and scoreboards (Redis, read by evolve_capabilities) are two stores
for one kind of fact: a task ran and this is what happened. This module reads
BOTH, gives every result the same shape keyed by the task id (a seeded census
goal's id is `census-<template>-<goal>`), and answers the question neither
store could: how has THIS task done across runs.

Read-only by design. The archive is the census baseline and is never
rewritten here; a result derived from it says `source: census` and names the
archive run it came from. Loaded after the census and evolve modules so it
can reach their readers through sys.modules (the loader registers modules by
bare filename).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import task_history_core as th
except ImportError:                                   # pragma: no cover
    from vera.evolve import task_history_core as th

log = logging.getLogger("vera.evolve.task_history")


def _mods():
    cc = sys.modules.get("census_capabilities")
    ev = sys.modules.get("evolve_capabilities")
    return cc, ev


def _rd(v: Any) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _census_results(cc, only_run: str = "") -> List[Dict[str, Any]]:
    """Every census archive row as a result. The archive's mtime is the
    timestamp fallback for rows written before rows carried one."""
    if cc is None:
        return []
    files = cc._run_files()
    out: List[Dict[str, Any]] = []
    items = [(rid, p) for rid, p in files.items() if not only_run or rid == only_run]
    recs = await asyncio.gather(*(cc._read_run(p) for _, p in items))
    for (rid, path), rows in zip(items, recs):
        try:
            fallback = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
        except Exception:
            fallback = ""
        excluded = ""
        try:
            excluded = cc.cc.name_marks_unusable(rid)
        except Exception:
            pass
        for row in rows or []:
            r = th.result_from_census_row(row, rid, ts_fallback=fallback)
            if r:
                r["excluded"] = excluded or ""
                out.append(r)
    return out


async def _suite_results(ev, templates: List[str]) -> List[Dict[str, Any]]:
    """Run records and suite scoreboard rows as results."""
    if ev is None:
        return []
    r = ev._redis()
    if not r:
        return []
    out: List[Dict[str, Any]] = []
    try:
        rows = await r.lrange(ev.KEY_RUNS, 0, ev.RUNS_CAP - 1)
        for raw in rows or []:
            try:
                rec = json.loads(_rd(raw))
            except Exception:
                continue
            res = th.result_from_run_record(rec, templates=templates)
            if res:
                out.append(res)
    except Exception as e:
        log.info("task history: run records unavailable: %s", e)
    try:
        suites = await r.lrange(ev.KEY_SUITES, 0, ev.SUITES_CAP - 1)
        for raw in suites or []:
            try:
                s = json.loads(_rd(raw))
            except Exception:
                continue
            for row in s.get("results") or []:
                rec = dict(row)
                rec.setdefault("ts", s.get("ts"))
                rec.setdefault("variant", s.get("variant"))
                res = th.result_from_run_record(rec, suite_id=str(s.get("suite_id") or ""),
                                                suite_tag=str(s.get("tag") or ""), templates=templates)
                if res:
                    out.append(res)
    except Exception as e:
        log.info("task history: suites unavailable: %s", e)
    return out


async def _tasks(ev) -> List[Dict[str, Any]]:
    if ev is None:
        return []
    try:
        return await ev._get_tasks()
    except Exception:
        return []


def _template_names(tasks: List[Dict[str, Any]]) -> List[str]:
    names = set()
    for t in tasks:
        c = t.get("census") if isinstance(t.get("census"), dict) else {}
        if c.get("template"):
            names.add(str(c["template"]))
    names.add("default")
    return sorted(names)


async def all_results(*, only_run: str = "") -> Dict[str, Any]:
    cc, ev = _mods()
    tasks = await _tasks(ev)
    templates = _template_names(tasks)
    census, suite = await asyncio.gather(_census_results(cc, only_run), _suite_results(ev, templates))
    merged = th.merge_results(suite, census)
    return {"results": merged, "tasks": tasks, "templates": templates,
            "sources": {"census": len(census), "suite_and_runs": len(suite), "merged": len(merged)}}


@capability(
    "evolve.results", memory="off", silent=True,
    http_method="GET", http_path="/evolve/results", http_tags=["evolve", "census"],
    description=(
        "EVERY task result Vera holds, in one shape, from every driver: census "
        "archive rows (source=census, driver=the archive run), suite scoreboard "
        "rows (source=suite, driver=suite_id), single task runs (source=task) and "
        "improvement rounds (source=improve, driver=variant). A census goal is the "
        "task census-<template>-<goal>. Read-only; the census archive is never "
        "rewritten. Filters: task (str), template (str), tag (str — census-<template> "
        "or a task tag), source (str), run (str — one census archive run), "
        "include_excluded (bool=false — partial/failed archive runs are left out by "
        "default, as census.runs does), limit (int=2000). Output: {results[], count, "
        "sources, templates}."),
)
async def cap_evolve_results(task: str = "", template: str = "", tag: str = "", source: str = "",
                             run: str = "", include_excluded: bool = False, limit: int = 2000,
                             trace_id=None) -> Dict[str, Any]:
    cc, ev = _mods()
    data = await all_results(only_run=run)
    rows = data["results"]
    inc = str(include_excluded).strip().lower() in ("1", "true", "yes", "on")
    if not inc:
        rows = [r for r in rows if not r.get("excluded")]
    if task:
        rows = [r for r in rows if r.get("task_id") == task]
    if template:
        rows = [r for r in rows if r.get("template") == template]
    if tag:
        tag_tasks = {t.get("id") for t in data["tasks"] if tag in (t.get("tags") or [])}
        rows = [r for r in rows if r.get("task_id") in tag_tasks or r.get("tag") == tag
                or (tag.startswith("census-") and r.get("template") == tag[len("census-"):])]
    if source:
        rows = [r for r in rows if r.get("source") == source]
    rows.sort(key=lambda r: (r.get("ts") or "", r.get("run_id") or ""), reverse=True)
    return {"results": rows[:max(1, int(limit))], "count": len(rows),
            "sources": data["sources"], "templates": data["templates"]}


@capability(
    "evolve.task.history", memory="off", silent=True,
    http_method="GET", http_path="/evolve/task/history", http_tags=["evolve", "census"],
    description=(
        "ONE task through time: its results from every driver, newest first, with "
        "the stats a per-task dashboard shows - runs, ok rate, capped, wall "
        "median/min/max and spread, quality mean, streak, ok-rate trend - computed "
        "over all results and again over the census-driven ones alone (the "
        "comparable series), per-driver stats, and the points in its history where "
        "the code it ran on changed. Inputs: id (str! — task id, e.g. "
        "census-default-build-multifile), include_excluded (bool=false). "
        "Output: {task_id, task, results[], stats, census_stats, by_driver, "
        "code_changes}."),
)
async def cap_evolve_task_history(id: str = "", include_excluded: bool = False,
                                  trace_id=None) -> Dict[str, Any]:
    tid = str(id or "").strip()
    if not tid:
        return {"error": "id required"}
    data = await all_results()
    rows = data["results"]
    inc = str(include_excluded).strip().lower() in ("1", "true", "yes", "on")
    if not inc:
        rows = [r for r in rows if not r.get("excluded")]
    hist = th.task_history(rows, tid)
    task = next((t for t in data["tasks"] if t.get("id") == tid), None)
    hist["task"] = task
    if task is None:
        parts = th.split_task_id(tid, data["templates"])
        hist["task"] = ({"id": tid, "label": parts.get("goal") or tid, "census": parts, "unregistered": True}
                        if parts else None)
    return hist


@capability(
    "evolve.tasks.overview", memory="off", silent=True,
    http_method="GET", http_path="/evolve/tasks/overview", http_tags=["evolve", "census"],
    description=(
        "One line per task with its history rolled up: runs, ok rate, capped, wall "
        "median, quality mean, streak, trend, the last result, and a short series "
        "(oldest first) for a sparkline. Tasks come from the task store (filter by "
        "tag / template); results from every driver. Inputs: tag (str), template "
        "(str), include_excluded (bool=false), limit (int=500). "
        "Output: {tasks[], count}."),
)
async def cap_evolve_tasks_overview(tag: str = "", template: str = "", include_excluded: bool = False,
                                    limit: int = 500, trace_id=None) -> Dict[str, Any]:
    data = await all_results()
    rows = data["results"]
    inc = str(include_excluded).strip().lower() in ("1", "true", "yes", "on")
    if not inc:
        rows = [r for r in rows if not r.get("excluded")]
    tasks = data["tasks"]
    if tag:
        tasks = [t for t in tasks if tag in (t.get("tags") or [])]
    if template:
        tasks = [t for t in tasks if (t.get("census") or {}).get("template") == template]
    ids = [t.get("id") for t in tasks if t.get("id")]
    # Results for tasks the store no longer holds (a deleted template) are
    # still history; include their ids so nothing silently vanishes.
    known = set(ids)
    extra = sorted({r["task_id"] for r in rows if r["task_id"] not in known
                    and (not tag or r.get("tag") == tag or (tag.startswith("census-") and r.get("template") == tag[7:]))
                    and (not template or r.get("template") == template)})
    ov = th.tasks_overview(rows, task_ids=ids + extra)
    by_id = {t.get("id"): t for t in tasks}
    for o in ov:
        t = by_id.get(o["task_id"])
        o["label"] = (t or {}).get("label") or o["task_id"]
        o["tags"] = (t or {}).get("tags") or []
        o["template"] = ((t or {}).get("census") or {}).get("template") or th.split_task_id(o["task_id"], data["templates"]).get("template", "")
        o["registered"] = t is not None
    ov.sort(key=lambda o: (o["template"] or "~", o["task_id"]))
    return {"tasks": ov[:max(1, int(limit))], "count": len(ov)}
