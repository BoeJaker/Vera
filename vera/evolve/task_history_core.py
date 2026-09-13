"""One task through time. Pure.

The census and the Loop Lab suite were never different systems: a census goal,
once seeded, IS a task (`census-<template>-<goal>`), and a census run is a set
of tasks run in order. What stayed split was the RESULTS: a census goal's
result is a row in a JSONL archive, a suite task's result is a run record in
Redis, and nothing put the two side by side - so "how has THIS task done over
the last ten runs" could not be asked at all.

This module gives both the same shape - a RESULT - keyed by task id, and rolls
a task's results up into the stats a per-task dashboard shows. It never writes
anything and never reads a file: the callers hand it census rows and run
records, it hands back results and stats. The census archive stays the
baseline it is; a result derived from it carries `source: census` and the run
it came from, so the two provenances are never confused.

    RESULT
      task_id      census-<template>-<goal>, or the suite's task id
      run_id       the loop session for a census goal; the run record's id otherwise
      source       census | suite | task | improve
      driver       {kind: census|suite|improve|task, id: run52 | suite_id | session}
      ts           ISO UTC when it ran (census rows before 2026-09-10 have none;
                   the caller may supply a fallback from the archive's mtime)
      status       done | wall-cap | error | skipped | ... (census words) or
                   pass | fail (suite words) - `ok` is the shared verdict
      ok           True when the harness/suite called it done and passing
      wall_s, elapsed_s, checks_ok, checks_n, pass_rate
      code         {sha_short, branch, changed} when recorded
      routing      {coder, nodes, spill_calls, reroutes} when recorded
      session      the loop session id (drill-down key)
      error        the failure text when any
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

CENSUS_PREFIX = "census-"
# census_seed.slug, repeated here so this module stays import-free: the id a
# result is keyed by must be the id the task store holds.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: Any) -> str:
    return _SLUG_RE.sub("-", str(name or "").strip().lower()).strip("-")


def census_task_id(template: str, goal_id: str) -> str:
    """The seeded task id for a census goal (census_seed.task_id_for's rule).
    An unlabelled row (before templates existed) belongs to `default`, which
    is what the archive's numbered series always was."""
    t = _slug(template) or "default"
    return "%s%s-%s" % (CENSUS_PREFIX, t, _slug(goal_id) or "goal")


def split_task_id(task_id: str, templates: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Inverse of census_task_id for ids that are census tasks; {} otherwise.

    Template names contain hyphens (`exec-family`), so the id cannot be split
    on its first one. With `templates` (the names the store knows) the LONGEST
    template that prefixes the rest wins; without them the first segment is
    taken, which is right only for single-word templates such as `default`.
    """
    s = str(task_id or "")
    if not s.startswith(CENSUS_PREFIX):
        return {}
    rest = s[len(CENSUS_PREFIX):]
    best = ""
    for t in (templates or []):
        t = str(t or "")
        if t and rest.startswith(t + "-") and len(t) > len(best):
            best = t
    if best:
        return {"template": best, "goal": rest[len(best) + 1:]}
    template, _, goal = rest.partition("-")
    return {"template": template, "goal": goal}


def result_from_census_row(row: Dict[str, Any], run_id: str, *,
                           ts_fallback: str = "") -> Optional[Dict[str, Any]]:
    """A census archive row as a RESULT. `run_id` is the archive's run id
    (run52, exec-family-run2, current...). Rows that never ran (skipped, no
    session) still become results - a task that could not run is a fact about
    the task's history, not a gap in it."""
    if not isinstance(row, dict) or not row.get("id"):
        return None
    q = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    status = str(row.get("status") or ("skipped" if row.get("skipped") else
                                       ("no-session" if row.get("no_session") else "unknown")))
    code = row.get("code") if isinstance(row.get("code"), dict) else {}
    cv = row.get("code_version") if isinstance(row.get("code_version"), list) else []
    if not code.get("sha") and cv:
        sha, _, branch = str(cv[0]).partition("@")
        code = {"sha": sha, "sha_short": sha[:10], "branch": branch}
    ro = row.get("routing") if isinstance(row.get("routing"), dict) else {}
    ro_calls = ro.get("calls") if isinstance(ro.get("calls"), dict) else {}
    ro_roles = ((ro.get("at_start") or {}).get("roles") or {}) if isinstance(ro.get("at_start"), dict) else {}
    wall = _num(row.get("wall_s"))
    checks_n = int(q.get("total") or 0)
    checks_ok = int(q.get("passed") or 0)
    return {
        "task_id": census_task_id(row.get("template") or "", row.get("id")),
        "goal": str(row.get("id")),
        "template": str(row.get("template") or "default"),
        "run_id": str(row.get("session_id") or "") or "%s:%s" % (run_id, row.get("id")),
        "source": "census",
        "driver": {"kind": "census", "id": str(run_id)},
        "ts": str(row.get("ended_at") or row.get("started_at") or ts_fallback or ""),
        "status": status,
        "ok": status == "done",
        "wall_s": wall,
        "elapsed_s": wall,
        "checks_ok": checks_ok, "checks_n": checks_n,
        "pass_rate": (round(checks_ok / checks_n, 3) if checks_n else None),
        "wall_cap_s": _num(row.get("wall_cap_s")),
        "hit_cap": status == "wall-cap",
        "code": ({"sha_short": str(code.get("sha_short") or code.get("sha") or "")[:10],
                  "branch": str(code.get("branch") or ""),
                  "changed": bool(code.get("changed_during_goal"))} if code.get("sha") else None),
        "routing": ({"coder": (ro_roles.get("coder") or {}).get("model") or "",
                     "nodes": ro_calls.get("nodes") or {},
                     "spill_calls": int(ro_calls.get("spill_calls") or 0),
                     "reroutes": len(ro_calls.get("reroutes") or [])} if ro_calls else None),
        "session": str(row.get("session_id") or ""),
        "error": str(row.get("error") or "")[:300],
        "reruns": len(row.get("reruns") or []),
        "planned": row.get("planned"), "executed": row.get("executed"),
        "tool_calls": row.get("tool_calls"),
        "warnings": len(row.get("warnings") or []),
        "model": str(row.get("model") or ""),
    }


def result_from_run_record(rec: Dict[str, Any], *, suite_id: str = "",
                           suite_tag: str = "",
                           templates: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    """A suite / task-run record (evolve.runs, or a suite scoreboard's result
    row) as a RESULT. A record the census harness ingested (source=census,
    result_ingest_core) reads as a census result - same driver, same facts as
    its archive row - and says `ingested` so the archive row can outrank it."""
    if not isinstance(rec, dict) or not rec.get("task"):
        return None
    if str(rec.get("source") or "") == "census" and rec.get("census_run") is not None:
        return _result_from_ingested(rec, suite_tag=suite_tag)
    err = str(rec.get("error") or "")
    pr = _num(rec.get("pass_rate"))
    checks_n = int(rec.get("checks_n") or 0)
    checks_ok = int(rec.get("checks_ok") or 0)
    if not checks_n and isinstance(rec.get("checks"), list):
        checks_n = len(rec["checks"])
        checks_ok = sum(1 for c in rec["checks"] if isinstance(c, dict) and c.get("ok"))
    ok = (not err) and (pr is None or pr >= 0.999) and (checks_n == 0 or checks_ok == checks_n)
    source = str(rec.get("source") or "")
    kind = "improve" if rec.get("variant") else ("suite" if (suite_id or source == "suite") else "task")
    parts = split_task_id(str(rec.get("task")), templates)
    return {
        "task_id": str(rec.get("task")),
        "goal": parts.get("goal") or "",
        "template": parts.get("template") or "",
        "run_id": str(rec.get("run_id") or ""),
        "source": kind,
        "driver": {"kind": kind, "id": str(suite_id or rec.get("variant") or rec.get("run_id") or "")},
        "ts": str(rec.get("ts") or ""),
        "status": ("error" if err else ("pass" if ok else "fail")),
        "ok": ok,
        "wall_s": _num(rec.get("elapsed_s")),
        "elapsed_s": _num(rec.get("elapsed_s")),
        "checks_ok": checks_ok, "checks_n": checks_n,
        "pass_rate": pr,
        "wall_cap_s": None,
        "hit_cap": False,
        "code": None,
        "routing": None,
        "session": str(rec.get("session") or ""),
        "error": err[:300],
        "reruns": 0,
        "planned": None, "executed": None, "tool_calls": None, "warnings": None,
        "model": str(rec.get("variant") or ""),
        "where": str(rec.get("where") or ""),
        "profile": str(rec.get("profile") or ""),
        "combined": _num(rec.get("combined")),
        "tag": suite_tag,
    }


def _result_from_ingested(rec: Dict[str, Any], *, suite_tag: str = "") -> Dict[str, Any]:
    """An ingested census run record (or a census suite scoreboard's row) as
    the census result its archive row would be. The run_id is the loop
    session in both, so merge_results sees them as one."""
    status = str(rec.get("status") or "unknown")
    # The record's error is the status when the row had no error text of its
    # own (the suite's readers need a non-empty error to see a problem); the
    # archive row's result keeps error and status apart, so undo that here.
    err = str(rec.get("error") or "")
    if err == status:
        err = ""
    checks_n = int(rec.get("checks_n") or 0)
    checks_ok = int(rec.get("checks_ok") or 0)
    wall = _num(rec.get("elapsed_s"))
    code = rec.get("code") if isinstance(rec.get("code"), dict) else None
    routing = rec.get("routing") if isinstance(rec.get("routing"), dict) else None
    goal = str(rec.get("goal_id") or rec.get("label") or "")
    template = str(rec.get("template") or "")
    if not goal or not template:
        parts = split_task_id(str(rec.get("task")), [template] if template else None)
        goal = goal or parts.get("goal") or ""
        template = template or parts.get("template") or ""
    return {
        "task_id": str(rec.get("task")),
        "goal": goal,
        "template": template,
        "run_id": str(rec.get("run_id") or ""),
        "source": "census",
        "driver": {"kind": "census", "id": str(rec.get("census_run") or "")},
        "ts": str(rec.get("ts") or ""),
        "status": status,
        "ok": bool(rec.get("ok", status == "done")),
        "wall_s": wall,
        "elapsed_s": wall,
        "checks_ok": checks_ok, "checks_n": checks_n,
        "pass_rate": (round(checks_ok / checks_n, 3) if checks_n else None),
        "wall_cap_s": _num(rec.get("wall_cap_s")),
        "hit_cap": bool(rec.get("hit_cap", status == "wall-cap")),
        "code": ({"sha_short": str(code.get("sha_short") or "")[:10], "branch": str(code.get("branch") or ""),
                  "changed": bool(code.get("changed"))} if code and code.get("sha_short") else None),
        "routing": ({"coder": str(routing.get("coder") or ""), "nodes": routing.get("nodes") or {},
                     "spill_calls": int(routing.get("spill_calls") or 0),
                     "reroutes": int(routing.get("reroutes") or 0)} if routing else None),
        "session": str(rec.get("loop_session") or ""),
        "error": err[:300],
        "reruns": int(rec.get("reruns") or 0),
        "planned": rec.get("planned"), "executed": rec.get("executed"),
        "tool_calls": rec.get("tool_calls"),
        "warnings": rec.get("warnings"),
        "model": str(rec.get("model") or ""),
        "combined": _num(rec.get("combined")),
        "tag": suite_tag,
        "ingested": True,
        "excluded": str(rec.get("excluded") or ""),
    }


def _outranks(new: Dict[str, Any], cur: Dict[str, Any]) -> bool:
    """Of two results for one (task, run): the census ARCHIVE row beats an
    ingested copy of itself, which beats a suite/task record for the same
    loop session - each carries more than the next."""
    if new["source"] != "census":
        return False
    if cur["source"] != "census":
        return True
    return bool(cur.get("ingested")) and not new.get("ingested")


def merge_results(*groups: Iterable[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """All results, deduplicated on (task_id, run_id) - a census row and a
    suite record for the same loop session are one result, and the census one
    wins because it carries more (wall cap, code, routing, quality detail);
    the archive row wins over its own ingested copy."""
    seen: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for g in groups:
        for r in g or []:
            if not r:
                continue
            k = (r["task_id"], r["run_id"])
            if k in seen:
                if _outranks(r, seen[k]):
                    seen[k] = r
                continue
            seen[k] = r
            order.append(k)
    return [seen[k] for k in order]


def archive_marks(run_ids: Iterable[str], marker_of: Callable[[str], str]) -> Dict[str, str]:
    """census run -> the exclusion marker its archive's NAME carries, for the
    archives that carry one: `run51-failed-coder-cpu-spill` -> {run51:
    "failed"}. An ingested record names its run by the bare id the harness
    reserved at start, so this is how it learns the archive was later marked
    unusable."""
    out: Dict[str, str] = {}
    for rid in run_ids or []:
        rid = str(rid or "")
        m = marker_of(rid) or ""
        if not m:
            continue
        i = rid.lower().find(m.lower())
        base = rid[:i].rstrip("-") if i > 0 else rid
        out[base] = m
    return out


_RUN_NUM_RE = re.compile(r"run(\d+)", re.I)


def order_key(r: Dict[str, Any]) -> tuple:
    """Chronological order for results. Time first; on a tie (rows written
    before rows carried a time all take their archive's end time, and the
    backfilled archives of one day share it) the census run NUMBER decides -
    the one honest ordering signal the archive names carry; the run id last,
    for a stable order."""
    drv = str((r.get("driver") or {}).get("id") or "")
    m = _RUN_NUM_RE.search(drv)
    return (r.get("ts") or "", int(m.group(1)) if m else 0, r.get("run_id") or "")


def task_history(results: Sequence[Dict[str, Any]], task_id: str) -> Dict[str, Any]:
    """One task's results, newest first, with the stats a dashboard shows.

    Stats are computed over ALL of the task's results and again over the
    census-driven ones alone, because those are the comparable series (the
    suite path runs a different engine per profile - see the census parity
    rules). A number that mixes them would be measuring two instruments.
    """
    rows = [r for r in results if r.get("task_id") == task_id]
    rows.sort(key=order_key, reverse=True)
    return {
        "task_id": task_id,
        "results": rows,
        "stats": _stats(rows),
        "census_stats": _stats([r for r in rows if r.get("source") == "census"]),
        "by_driver": _by_driver(rows),
        "code_changes": _code_changes(rows),
    }


def tasks_overview(results: Sequence[Dict[str, Any]], *, task_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """One line per task: last result, runs, pass rate, wall median, trend."""
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by.setdefault(r["task_id"], []).append(r)
    ids = list(task_ids) if task_ids is not None else sorted(by)
    out = []
    for tid in ids:
        rows = sorted(by.get(tid, []), key=order_key, reverse=True)
        st = _stats(rows)
        last = rows[0] if rows else None
        out.append({"task_id": tid, "runs": len(rows), "stats": st,
                    "last": ({"ts": last.get("ts"), "status": last.get("status"), "ok": last.get("ok"),
                              "wall_s": last.get("wall_s"), "driver": last.get("driver"),
                              "run_id": last.get("run_id")} if last else None),
                    "series": [{"ts": r.get("ts"), "ok": r.get("ok"), "wall_s": r.get("wall_s"),
                                "status": r.get("status"), "driver": (r.get("driver") or {}).get("id"),
                                "code": (r.get("code") or {}).get("sha_short") if r.get("code") else None}
                               for r in reversed(rows[-24:])]})
    return out


# ── helpers ──────────────────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if not n:
        return {"runs": 0}
    oks = sum(1 for r in rows if r.get("ok"))
    caps = sum(1 for r in rows if r.get("hit_cap"))
    walls = [r["wall_s"] for r in rows if r.get("wall_s") is not None]
    prs = [r["pass_rate"] for r in rows if r.get("pass_rate") is not None]
    recent = rows[:5]
    earlier = rows[5:10]
    trend = None
    if recent and earlier:
        a = sum(1 for r in recent if r.get("ok")) / len(recent)
        b = sum(1 for r in earlier if r.get("ok")) / len(earlier)
        trend = round(a - b, 3)
    return {
        "runs": n, "ok": oks, "capped": caps, "failed": n - oks - caps,
        "ok_rate": round(oks / n, 3),
        "wall_median_s": (round(statistics.median(walls), 1) if walls else None),
        "wall_min_s": (round(min(walls), 1) if walls else None),
        "wall_max_s": (round(max(walls), 1) if walls else None),
        "wall_spread": (round(max(walls) / min(walls), 2) if walls and min(walls) > 0 else None),
        "quality_mean": (round(sum(prs) / len(prs), 3) if prs else None),
        "trend_ok_rate": trend,
        "last_ok": bool(rows[0].get("ok")),
        "streak": _streak(rows),
    }


def _streak(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How many of the newest results share the newest verdict."""
    if not rows:
        return {"n": 0, "ok": None}
    first = bool(rows[0].get("ok"))
    n = 0
    for r in rows:
        if bool(r.get("ok")) != first:
            break
        n += 1
    return {"n": n, "ok": first}


def _by_driver(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str((r.get("driver") or {}).get("kind") or r.get("source")), []).append(r)
    return {k: _stats(v) for k, v in out.items()}


def _code_changes(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Where, in this task's history (oldest first), the code it ran on
    changed - the boundaries a before/after comparison must respect."""
    seq = [r for r in reversed(rows) if r.get("code") and r["code"].get("sha_short")]
    out = []
    prev = None
    for r in seq:
        tag = "%s@%s" % (r["code"]["sha_short"], r["code"].get("branch") or "?")
        if prev is not None and tag != prev:
            out.append({"at": r.get("run_id"), "driver": (r.get("driver") or {}).get("id"),
                        "ts": r.get("ts"), "from": prev, "to": tag})
        prev = tag
    return out
