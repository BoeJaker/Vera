"""evolve.results / evolve.task.history / evolve.tasks.overview — one task
through time, across every driver that ran it.

The census archive (JSONL, read by census_capabilities) and the suite's run
records and scoreboards (Redis, read by evolve_capabilities) are two stores
for one kind of fact: a task ran and this is what happened. This module reads
BOTH, gives every result the same shape keyed by the task id (a seeded census
goal's id is `census-<template>-<goal>`), and answers the question neither
store could: how has THIS task done across runs.

The archive is the census baseline and is never rewritten here; a result
derived from it says `source: census` and names the archive run it came from.
The one write is `evolve.result.ingest`, which goes the OTHER way: the census
harness posts each finished goal and it becomes a run record in the suite
store, a finished census run a suite scoreboard tagged census-<template>
(result_ingest_core) - so the suite's own views see census results natively
and a census run is a driver run like any other. Loaded after the census and
evolve modules so it can reach their readers through sys.modules (the loader
registers modules by bare filename).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.evolve import task_history_core as th
    from Vera.vera.evolve import result_ingest_core as ric
    from Vera.vera.evolve import work_core as wc
    from Vera.vera.evolve import loop_record_core as lrc
except ImportError:                                   # pragma: no cover
    from vera.evolve import task_history_core as th
    from vera.evolve import result_ingest_core as ric
    from vera.evolve import work_core as wc
    from vera.evolve import loop_record_core as lrc

log = logging.getLogger("vera.evolve.task_history")

_DETAIL_TTL_S = 14 * 86400   # evolve_capabilities._push_run's own detail lifetime


def _mods():
    cc = sys.modules.get("census_capabilities")
    ev = sys.modules.get("evolve_capabilities")
    return cc, ev


def _rd(v: Any) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


def archive_time(path) -> str:
    """When an archive's run ended, as ISO UTC, for rows written before rows
    carried their own time. The harness's `.log` beside the archive is
    written as the run goes and never touched again, so its mtime is the
    end of the run; the `.jsonl` is rewritten by the backfill tools
    (code_version, reroutes) and its mtime is whenever that last happened.
    Prefer the log; fall back to the archive itself."""
    try:
        p = path if hasattr(path, "with_suffix") else Path(str(path))
        lg = p.with_suffix(".log")
        st = lg.stat() if lg.exists() else p.stat()
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
    except Exception:
        return ""


async def _census_results(cc, only_run: str = "") -> List[Dict[str, Any]]:
    """Every census archive row as a result. The archive's end time (its
    log's mtime) is the timestamp fallback for rows written before rows
    carried one."""
    if cc is None:
        return []
    files = cc._run_files()
    out: List[Dict[str, Any]] = []
    items = [(rid, p) for rid, p in files.items() if not only_run or rid == only_run]
    recs = await asyncio.gather(*(cc._read_run(p) for _, p in items))
    for (rid, path), rows in zip(items, recs):
        fallback = archive_time(path)
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


def _archive_marks(cc) -> Dict[str, str]:
    """census run -> exclusion marker, from the archive names on disk."""
    if cc is None:
        return {}
    try:
        return th.archive_marks(cc._run_files().keys(), cc.cc.name_marks_unusable)
    except Exception:
        return {}


async def _suite_results(ev, templates: List[str], marks: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Run records and suite scoreboard rows as results. An ingested census
    record is excluded when its archive was later marked unusable (`marks`)
    or when it was ingested from an archive already carrying a marker."""
    if ev is None:
        return []
    r = ev._redis()
    if not r:
        return []
    marks = marks or {}
    out: List[Dict[str, Any]] = []

    def _take(res: Optional[Dict[str, Any]]):
        if not res:
            return
        if res.get("ingested"):
            res["excluded"] = res.get("excluded") or marks.get((res.get("driver") or {}).get("id") or "", "")
        out.append(res)

    try:
        rows = await r.lrange(ev.KEY_RUNS, 0, ev.RUNS_CAP - 1)
        for raw in rows or []:
            try:
                rec = json.loads(_rd(raw))
            except Exception:
                continue
            _take(th.result_from_run_record(rec, templates=templates))
    except Exception as e:
        log.info("task history: run records unavailable: %s", e)
    try:
        suites = await r.lrange(ev.KEY_SUITES, 0, ev.SUITES_CAP - 1)
        for raw in suites or []:
            try:
                s = json.loads(_rd(raw))
            except Exception:
                continue
            census_suite = str(s.get("source") or "") == "census"
            for row in s.get("results") or []:
                rec = dict(row)
                rec.setdefault("ts", s.get("ts"))
                rec.setdefault("variant", s.get("variant"))
                if census_suite:
                    # The scoreboard's rows are the run records in brief; the
                    # facts that make them census results sit on the suite.
                    rec.setdefault("source", "census")
                    rec.setdefault("census_run", s.get("census_run") or s.get("suite_id"))
                    rec.setdefault("template", s.get("template"))
                    rec.setdefault("excluded", s.get("excluded"))
                _take(th.result_from_run_record(rec, suite_id=str(s.get("suite_id") or ""),
                                                suite_tag=str(s.get("tag") or ""), templates=templates))
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
    census, suite = await asyncio.gather(_census_results(cc, only_run),
                                         _suite_results(ev, templates, _archive_marks(cc)))
    merged = th.merge_results(suite, census)
    return {"results": merged, "tasks": tasks, "templates": templates,
            "sources": {"census": len(census), "suite_and_runs": len(suite), "merged": len(merged),
                        "ingested": sum(1 for r in suite if r.get("ingested"))}}


# ── the write side: a census goal becomes a run record ───────────────────────

def insert_position(rows: List[Dict[str, Any]], ts: str, cap: int) -> Optional[int]:
    """Where a record with time `ts` belongs in the newest-first run list:
    the index of the first entry no newer than it (insert before that one),
    len(rows) to append at the tail, or None when the list is full and every
    entry is newer - the record is beyond the window and adding it would only
    evict something more recent.

    The suite's own records are pushed as they finish, so the list was always
    in time order; a backfilled census archive is not "now" and must take its
    place by time, or the Runs view interleaves days (seen 2026-09-10: the
    first backfill put every archive at the head, dated by ingest time)."""
    for i, rec in enumerate(rows):
        if str(rec.get("ts") or "") <= ts:
            return i
    return None if len(rows) >= cap else len(rows)


async def _upsert_run(ev, compact: Dict[str, Any], detail: Dict[str, Any],
                      may_replace=None) -> str:
    """Store a run record: replaced in place when the list already holds its
    run_id (the harness may post a goal twice: once live, again from the
    archive), otherwise inserted at its place by time. `may_replace(existing)`
    returning False keeps the stored record (a loop record never replaces the
    census record of the same session). Returns 'added' | 'replaced' | 'kept'
    | 'beyond_window' | '' (no store)."""
    r = ev._redis()
    if not r:
        return ""
    rid = compact["run_id"]
    raws = await r.lrange(ev.KEY_RUNS, 0, ev.RUNS_CAP - 1) or []
    recs: List[Dict[str, Any]] = []
    for raw in raws:
        try:
            recs.append(json.loads(_rd(raw)))
        except Exception:
            recs.append({})
    body = json.dumps(compact, default=str)
    for i, rec in enumerate(recs):
        if rec.get("run_id") == rid:
            if may_replace is not None and not may_replace(rec):
                return "kept"
            await r.lset(ev.KEY_RUNS, i, body)
            await r.set(ev.KEY_RUN + rid, json.dumps(detail, default=str))
            await r.expire(ev.KEY_RUN + rid, _DETAIL_TTL_S)
            return "replaced"
    pos = insert_position(recs, str(compact.get("ts") or ""), ev.RUNS_CAP)
    if pos is None:
        return "beyond_window"
    if pos == 0:
        await ev._push_run(compact, detail)          # the newest: the suite's own path
        return "added"
    if pos >= len(recs):
        await r.rpush(ev.KEY_RUNS, body)
    else:
        await r.linsert(ev.KEY_RUNS, "BEFORE", raws[pos], body)
    await r.ltrim(ev.KEY_RUNS, 0, ev.RUNS_CAP - 1)
    await r.set(ev.KEY_RUN + rid, json.dumps(detail, default=str))
    await r.expire(ev.KEY_RUN + rid, _DETAIL_TTL_S)
    return "added"


async def _upsert_suite(ev, summary: Dict[str, Any]) -> str:
    """Store a census run's scoreboard: replaced in place when the list holds
    its suite_id, otherwise at its place by time (the same rule as the run
    list - a backfilled archive is not the latest suite). Returns 'added' |
    'replaced' | 'beyond_window' | ''."""
    r = ev._redis()
    if not r:
        return ""
    sid = summary["suite_id"]
    raws = await r.lrange(ev.KEY_SUITES, 0, ev.SUITES_CAP - 1) or []
    recs: List[Dict[str, Any]] = []
    for raw in raws:
        try:
            recs.append(json.loads(_rd(raw)))
        except Exception:
            recs.append({})
    body = json.dumps(summary, default=str)
    for i, rec in enumerate(recs):
        if rec.get("suite_id") == sid:
            await r.lset(ev.KEY_SUITES, i, body)
            return "replaced"
    pos = insert_position(recs, str(summary.get("ts") or ""), ev.SUITES_CAP)
    if pos is None:
        return "beyond_window"
    if pos == 0:
        await r.lpush(ev.KEY_SUITES, body)
    elif pos >= len(recs):
        await r.rpush(ev.KEY_SUITES, body)
    else:
        await r.linsert(ev.KEY_SUITES, "BEFORE", raws[pos], body)
    await r.ltrim(ev.KEY_SUITES, 0, ev.SUITES_CAP - 1)
    return "added"


def _as_rows(row: Any, rows: Any) -> List[Dict[str, Any]]:
    """The goal rows an ingest call carries: `rows` (a list, or a JSON string
    of one) and/or `row` (one, or its JSON string)."""
    out: List[Dict[str, Any]] = []
    for v in (rows, row):
        if isinstance(v, str) and v.strip():
            try:
                v = json.loads(v)
            except Exception:
                continue
        if isinstance(v, dict):
            out.append(v)
        elif isinstance(v, list):
            out.extend(x for x in v if isinstance(x, dict))
    return out


@capability(
    "evolve.result.ingest", memory="off", silent=True,
    http_method="POST", http_path="/evolve/result/ingest", http_tags=["evolve", "census"],
    description=(
        "Record census goal results in the suite store, so a census run is a driver "
        "run like a suite: each goal row becomes a run record (source=census, "
        "run_id=its loop session, task=census-<template>-<goal>) that evolve.runs, "
        "evolve.run.get, evolve.activity and the task-through-time index read; with "
        "suite=true the rows also become a suite scoreboard (suite_id=census_run, "
        "tag=census-<template>) that evolve.suites, evolve.report and evolve.board "
        "read. Idempotent: a record with the same run_id / suite_id is replaced. The "
        "census harness (loop-census/run_census.py) posts each goal as it finishes; "
        "loop-census/ingest_census.py posts an archived run whole. Inputs: template "
        "(str!), census_run (str! - the archive id: run52, exec-family-run3), row "
        "(dict - one goal row) and/or rows (list), suite (bool=false), archive (str - "
        "the archive file name once known), excluded (str - the marker the archive "
        "name carries, e.g. failed), profile (str), ts_fallback (str - ISO UTC time "
        "for rows that carry none, e.g. the archive's end time; else now), source "
        "(str=census; nothing else is accepted). Run records are kept only for rows "
        "inside the store's detail window (14 days): an older row would sit in the "
        "run list with no detail and push a live record out, for a history the "
        "archive already holds; its suite scoreboard is still written. Output: {ok, "
        "ingested, added, replaced, skipped_old, run_ids, suite_id, tag}."),
)
async def cap_evolve_result_ingest(template: str = "", census_run: str = "", row: Any = None,
                                   rows: Any = None, suite: Any = False, archive: str = "",
                                   excluded: str = "", profile: str = "", ts_fallback: str = "",
                                   source: str = "census", trace_id=None) -> Dict[str, Any]:
    if str(source or "census") != "census":
        return {"error": "only source=census is ingested here; suite and task runs record themselves"}
    template = str(template or "").strip()
    census_run = str(census_run or "").strip()
    if not template or not census_run:
        return {"error": "template and census_run are required (e.g. default / run52)"}
    goal_rows = _as_rows(row, rows)
    if not goal_rows:
        return {"error": "no goal rows: pass row (one) or rows (a list)"}
    cc, ev = _mods()
    if ev is None:
        return {"error": "the suite store (evolve_capabilities) is not loaded"}
    if not ev._redis():
        # Say so rather than answer ok with nothing kept: the harness reads ok
        # as "recorded" and would stop trying.
        return {"error": "the suite store has no Redis here; nothing recorded"}
    tasks = await _tasks(ev)
    by_id = {t.get("id"): t for t in tasks}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fallback = str(ts_fallback or "").strip() or now
    horizon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - _DETAIL_TTL_S))
    want_suite = str(suite).strip().lower() in ("1", "true", "yes", "on")
    added = replaced = skipped_old = 0
    run_ids: List[str] = []
    for gr in goal_rows:
        tid = th.census_task_id(template, gr.get("id"))
        task = by_id.get(tid) or {}
        built = ric.run_record_from_census_row(
            gr, template=template, census_run=census_run, ts_fallback=fallback,
            goal_text=str(task.get("goal") or ""), profile=str(profile or task.get("profile") or ""),
            ingested_at=now)
        if not built:
            continue
        compact, detail = built
        if excluded:
            compact["excluded"] = detail["excluded"] = str(excluded)
        if compact["ts"] and compact["ts"] < horizon:
            skipped_old += 1
            continue
        outcome = await _upsert_run(ev, compact, detail)
        if outcome == "beyond_window":
            # The list is full of newer records: this one belongs to the
            # archive's history, not the store's window.
            skipped_old += 1
            continue
        if outcome == "added":
            added += 1
        elif outcome == "replaced":
            replaced += 1
        run_ids.append(compact["run_id"])
        if outcome == "added":
            try:
                await ev.emit_event({"type": "evolve.run.done", "run_id": compact["run_id"],
                                     "task": compact["task"], "pass_rate": compact["pass_rate"],
                                     "combined": compact["combined"], "elapsed_s": compact["elapsed_s"],
                                     "where": compact["where"], "error": compact["error"][:120],
                                     "source": "census", "census_run": census_run})
            except Exception:
                pass
    out: Dict[str, Any] = {"ok": True, "ingested": added + replaced, "added": added, "replaced": replaced,
                           "skipped_old": skipped_old, "run_ids": run_ids, "template": template,
                           "census_run": census_run, "tag": ric.census_tag(template), "suite_id": ""}
    if want_suite:
        summary = ric.suite_record_from_rows(goal_rows, template=template, census_run=census_run,
                                             archive=archive, excluded=excluded, profile=profile,
                                             ts_fallback=fallback, ingested_at=now)
        if summary:
            out["suite"] = await _upsert_suite(ev, summary)
            out["suite_id"] = summary["suite_id"]
            out["tasks_n"] = summary["tasks_n"]
    log.info("census ingest: %s/%s %d row(s) (%d added, %d replaced, %d older than the window)%s",
             template, census_run, len(run_ids), added, replaced, skipped_old,
             (" + suite %s" % out.get("suite")) if want_suite else "")
    return out


def _decode_events(raws) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in raws or []:
        try:
            e = json.loads(_rd(raw))
        except Exception:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


@capability(
    "evolve.loop.record", memory="off", silent=True,
    http_method="POST", http_path="/evolve/loop/record", http_tags=["evolve", "loop"],
    description=(
        "Record a finished agent loop in Loop Lab's run store, whoever started it "
        "(chat, dream, a v8 program, the API). Built from the loop's own event log "
        "(vera:loop:events:<session>) with the census's trace digest: engine, origin, "
        "tier, intent, plan style, planned/executed/inserted steps, tool calls, "
        "warnings, wall time, status. run_id = the loop session, source=loop, "
        "task=loop:<origin>. Called automatically when a loop ends; callable by hand "
        "to record a session still inside the event window (7 days). Never replaces "
        "a census or task record of the same session, and skips evolve:<run> "
        "sessions (Loop Lab tasks record themselves). Inputs: session_id (str!), "
        "where (str - the Vera process that ran it). Output: {ok, outcome "
        "(added|replaced|kept|beyond_window|skipped), run_id, origin}."),
)
async def cap_evolve_loop_record(session_id: str = "", where: str = "", trace_id=None) -> Dict[str, Any]:
    sid = str(session_id or "").strip()
    if not sid:
        return {"error": "session_id is required"}
    if lrc.is_self_recording(sid):
        return {"ok": True, "outcome": "skipped", "run_id": sid,
                "reason": "a Loop Lab task records its own run"}
    _, ev = _mods()
    if ev is None or not ev._redis():
        return {"error": "the suite store (evolve_capabilities) or its Redis is not available"}
    r = ev._redis()
    raws = await r.lrange("vera:loop:events:%s" % sid, 0, -1)
    events = _decode_events(raws)
    if not events:
        return {"ok": True, "outcome": "skipped", "run_id": sid, "reason": "no events for this session"}
    state_raw = await r.hgetall("vera:loop:run:%s" % sid) or {}
    run_state = {_rd(k): _rd(v) for k, v in state_raw.items()}
    try:
        from Vera.vera.dag import loop_trace_core as ltc
    except ImportError:                                   # pragma: no cover
        from vera.dag import loop_trace_core as ltc
    # The digest folds a few thousand events - off the event loop.
    digest = await asyncio.to_thread(ltc.digest_events, events)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    built = lrc.run_record_from_events(sid, events, digest, run_state=run_state,
                                       where=where, ingested_at=now)
    if not built:
        return {"ok": True, "outcome": "skipped", "run_id": sid, "reason": "the loop has not finished"}
    compact, detail = built
    outcome = await _upsert_run(ev, compact, detail, may_replace=lrc.may_replace)
    if outcome == "added":
        try:
            await ev.emit_event({"type": "evolve.run.done", "run_id": compact["run_id"],
                                 "task": compact["task"], "pass_rate": compact["pass_rate"],
                                 "combined": compact["combined"], "elapsed_s": compact["elapsed_s"],
                                 "where": compact["where"], "error": compact["error"][:120],
                                 "source": lrc.SOURCE, "origin": compact["origin"]})
        except Exception:
            pass
    return {"ok": True, "outcome": outcome, "run_id": sid, "origin": compact["origin"]}


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
    rows.sort(key=th.order_key, reverse=True)
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
        # The definition's own facts, so the Work table's tasks view is one
        # call: type, profile/cap, enabled, how many checks, a seeded goal's
        # template link and model override (the fields the old editor lost).
        if t is not None:
            o["type"] = str(t.get("type") or "loop")
            o["profile"] = str(t.get("profile") or "")
            o["cap"] = str(t.get("cap") or "")
            o["enabled"] = t.get("enabled") is not False
            o["checks_n"] = len(t.get("checks") or [])
            o["seeded"] = isinstance(t.get("census"), dict) and bool(t.get("census"))
            o["overrides"] = t.get("overrides") if isinstance(t.get("overrides"), dict) else None
            o["goal"] = str(t.get("goal") or "")[:200]
    ov.sort(key=lambda o: (o["template"] or "~", o["task_id"]))
    return {"tasks": ov[:max(1, int(limit))], "count": len(ov)}


# ── the Work page: one table of driver runs, one poll of what is live ────────

async def _safe(coro, default):
    try:
        return await coro
    except Exception as e:
        log.info("work: a reader failed: %s", e)
        return default


@capability(
    "evolve.work.drivers", memory="off", silent=True,
    http_method="GET", http_path="/evolve/work/drivers", http_tags=["evolve", "census"],
    description=(
        "Every DRIVER RUN Loop Lab knows, in one shape, newest first, the live one "
        "first: census runs (the archive, with routing/code/provenance rollups), suite "
        "scoreboards, improvement sessions and single runs (run/manual/goal/captest/ide "
        "- a run that belongs to a suite, session or census run is not a row). A census "
        "run the harness also posted as a scoreboard is ONE row (also_in_store=true). "
        "This is the Work page's runs view. Filters: kind (census|suite|improve|run), "
        "template, source, text, include_excluded (bool=true), limit (int=400), slim (bool - drop "
        "the census rollup's duplicate provenance and cut helpers to name + purpose: ~1.2 MB -> ~0.2 MB). "
        "Output: {drivers[], count, kinds{}, templates[], census_meta}."),
)
async def cap_evolve_work_drivers(kind: str = "", template: str = "", source: str = "", text: str = "",
                                  include_excluded: Any = True, limit: int = 400, slim: Any = False,
                                  trace_id=None) -> Dict[str, Any]:
    cc, ev = _mods()
    census = await _safe(cc.cap_census_runs(include_partial=True), {}) if cc is not None else {}
    suites = await _safe(ev.evolve_suites(limit=ev.SUITES_CAP), {}) if ev is not None else {}
    sessions = await _safe(ev.evolve_improve_list(limit=ev.SESSIONS_CAP), {}) if ev is not None else {}
    runs = await _safe(ev.evolve_runs(limit=ev.RUNS_CAP), {}) if ev is not None else {}
    live = False
    if cc is not None:
        try:
            active = await asyncio.to_thread(cc._read_active_sync)
            live = bool((active or {}).get("live"))
        except Exception:
            live = False
    rows = wc.driver_rows(census.get("runs") or [], suites.get("suites") or [],
                          sessions.get("sessions") or [], runs.get("runs") or [], live_census=live)
    inc = str(include_excluded).strip().lower() not in ("0", "false", "no", "off")
    shown = wc.filter_rows(rows, kind=kind, template=template, text=text, include_excluded=inc, source=source)
    kinds: Dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    templates = sorted({r["template"] for r in rows if r.get("template")})
    meta = {k: v for k, v in census.items() if k != "runs"}
    out = shown[:max(1, int(limit))]
    if str(slim).strip().lower() in ("1", "true", "yes", "on"):
        out = [wc.slim_driver(r) for r in out]
    return {"drivers": out, "count": len(shown), "total": len(rows),
            "kinds": kinds, "templates": templates, "census_meta": meta}


@capability(
    "evolve.work.live", memory="off", silent=True,
    http_method="GET", http_path="/evolve/work/live", http_tags=["evolve", "census"],
    description=(
        "What is running NOW, in one call, for the Work page's single poller: the "
        "census (census.live: harness, control, the goal in flight, recent goals), the "
        "suite in progress (evolve.suite.status), the live improvement session, and "
        "the active single run (evolve.run.status). Output: {census, suite, improve, "
        "run, any_live}."),
)
async def cap_evolve_work_live(trace_id=None) -> Dict[str, Any]:
    cc, ev = _mods()
    census = await _safe(cc.cap_census_live(), {}) if cc is not None else {}
    suite = await _safe(ev.evolve_suite_status(), {}) if ev is not None else {}
    sessions = await _safe(ev.evolve_improve_list(limit=5), {}) if ev is not None else {}
    run = await _safe(ev.evolve_run_status(), {}) if ev is not None else {}
    improve = next((s for s in (sessions.get("sessions") or [])
                    if s.get("live") or str(s.get("status") or "") == "running"), None)
    hz = census.get("harness") if isinstance(census.get("harness"), dict) else {}
    any_live = bool(census.get("active") or hz.get("live") or (suite.get("status") or {}).get("running")
                    or suite.get("running") or improve or run.get("live") or run.get("running"))
    return {"census": census, "suite": suite, "improve": improve, "run": run, "any_live": any_live}
