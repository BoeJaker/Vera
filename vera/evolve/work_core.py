"""The Work page's rows - one shape for every DRIVER RUN, whoever drove it.

Loop Lab held the same fact in four places: a census run was a row of the
census table, a suite a row of the Suite page, an improvement session a row
of the Improve page, and a single run a row of the Runs page - four tables,
four shapes, four pollers. The Work page (flattening slice 3) is ONE table
whose rows are tasks or, by toggle, driver runs; this module gives every
driver run the same row so that table can hold them all:

    DRIVER {kind: census|suite|improve|run, id, label, template, tag,
            profile, ts, ended_at (epoch, for sorting), status
            (running|done|failed|excluded|...), live, excluded (marker),
            tasks_n, done, failed, capped, pass_rate, avg_combined,
            quality_mean, wall_total_s, code {sha_short, branch}, routing,
            provenance, warnings, reruns, source, task, run_id, open
            {kind, id}  - what a click opens}

Pure: the callers hand it what the census, suite, improve and run readers
return; it hands back rows. A census run and the suite scoreboard the harness
posted for it (evolve.result.ingest, slice 2) are ONE row - the archive's,
which carries more - so a census run is never listed twice.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..census.census_core import name_marks_unusable

#: A single run whose source is one of these belongs to a parent driver row
#: (its suite, its improvement session, its census run) and is not a row of
#: its own.
CHILD_SOURCES = ("suite", "improve", "census")


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch(iso: Any) -> float:
    """ISO UTC -> epoch seconds, 0 when unreadable."""
    s = str(iso or "").strip()
    if not s:
        return 0.0
    try:
        base = s[:19]
        t = time.strptime(base, "%Y-%m-%dT%H:%M:%S")
        return float(time.mktime(t) - time.timezone)
    except Exception:
        return 0.0


def _iso(epoch: Any) -> str:
    e = _num(epoch)
    if not e:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))


def census_row(s: Dict[str, Any], *, live: bool = False) -> Dict[str, Any]:
    """A census run summary (census.runs / census_core.summarise_run, with
    the routing rollup and reruns census_capabilities adds) as a driver row.
    The summary is carried whole under `census` so the census table's own
    renderer keeps every column it had."""
    rid = str(s.get("run_id") or "")
    goals = int(s.get("goals") or 0)
    done = int(s.get("done") or 0)
    capped = int(s.get("wall_capped") or 0)
    # `excluded` is the MARKER (failed, partial, ...) so a filter can key on it;
    # the archive reader's sentence about it rides along as excluded_reason.
    excluded = ""
    if s.get("excluded"):
        excluded = name_marks_unusable(rid) or ("partial" if s.get("partial") else "excluded")
    is_live = live or rid == "current"
    ended = _num(s.get("ended_at")) or 0.0
    # census_core.code_rollup: segments of goals per sha@branch, in run order;
    # the last segment is the code the run ended on.
    code = s.get("code") if isinstance(s.get("code"), dict) else {}
    segs = code.get("segments") if isinstance(code.get("segments"), list) else []
    seg = segs[-1] if segs else {}
    return {
        "kind": "census", "id": rid, "label": rid,
        "template": str(s.get("template") or ""), "tag": ("census-%s" % s.get("template")) if s.get("template") else "",
        "profile": "",
        "ts": _iso(ended), "ended_at": ended,
        "status": ("running" if is_live else ("excluded" if excluded else ("partial" if s.get("partial") else "done"))),
        "live": is_live, "excluded": excluded, "excluded_reason": str(s.get("exclude_reason") or ""),
        "tasks_n": goals, "done": done, "failed": int(s.get("other") or 0), "capped": capped,
        "pass_rate": (round(done / goals, 3) if goals else None),
        "avg_combined": None,
        "quality_mean": _num(s.get("quality_mean")),
        "wall_total_s": _num(s.get("wall_total_s")),
        "code": ({"sha_short": str(seg.get("sha") or "")[:10], "branch": str(seg.get("branch") or ""),
                  "changed": bool(code.get("changed"))} if seg else None),
        "routing": s.get("routing") if isinstance(s.get("routing"), dict) else None,
        "provenance": s.get("provenance") if isinstance(s.get("provenance"), dict) else None,
        "warnings": int(s.get("warnings_total") or 0),
        "reruns": int(s.get("reruns") or 0),
        "source": "census", "task": "", "run_id": "",
        "open": {"kind": "census", "id": rid},
        "census": s,
    }


def suite_row(s: Dict[str, Any]) -> Dict[str, Any]:
    results = [r for r in (s.get("results") or []) if isinstance(r, dict)]
    n = len(results) or int(s.get("tasks_n") or 0)
    failed = sum(1 for r in results if r.get("error") or (_num(r.get("pass_rate")) is not None and r["pass_rate"] < 0.999))
    capped = sum(1 for r in results if r.get("hit_cap"))
    ts = str(s.get("ts") or "")
    return {
        "kind": "suite", "id": str(s.get("suite_id") or ""), "label": str(s.get("tag") or s.get("profile") or "suite"),
        "template": "", "tag": str(s.get("tag") or ""), "profile": str(s.get("profile") or ""),
        "ts": ts, "ended_at": _epoch(ts),
        "status": "done" if n else "empty", "live": False, "excluded": str(s.get("excluded") or ""),
        "tasks_n": n, "done": n - failed, "failed": failed, "capped": capped,
        "pass_rate": _num(s.get("pass_rate")), "avg_combined": _num(s.get("avg_combined")),
        "quality_mean": None, "wall_total_s": round(sum((_num(r.get("elapsed_s")) or 0) for r in results), 1) if results else None,
        "code": None, "routing": None, "provenance": None,
        "warnings": 0, "reruns": 0,
        "source": "suite", "task": "", "run_id": "",
        "critic": str(s.get("critic") or ""), "variant": str(s.get("variant") or ""),
        "open": {"kind": "suite", "id": str(s.get("suite_id") or "")},
        "results": results,
    }


def improve_row(s: Dict[str, Any]) -> Dict[str, Any]:
    live = bool(s.get("live")) or str(s.get("status") or "") == "running"
    ts = str(s.get("ended_at") or s.get("started_at") or "")
    rounds_done = int(s.get("rounds_done") or 0)
    return {
        "kind": "improve", "id": str(s.get("id") or ""), "label": str(s.get("profile") or "improve"),
        "template": "", "tag": "", "profile": str(s.get("profile") or ""),
        "ts": ts, "ended_at": _epoch(ts),
        "status": ("running" if live else str(s.get("status") or "done")), "live": live, "excluded": "",
        "tasks_n": rounds_done, "done": rounds_done, "failed": (1 if s.get("error") else 0), "capped": 0,
        "pass_rate": None, "avg_combined": _num(s.get("best_score")),
        "quality_mean": None, "wall_total_s": None,
        "code": None, "routing": None, "provenance": None,
        "warnings": 0, "reruns": 0,
        "source": "improve", "task": "", "run_id": "",
        "rounds": "%d/%s" % (rounds_done, s.get("max_rounds") or "?"),
        "best_variant": str(s.get("best_variant") or ""), "current": str(s.get("current") or ""),
        "error": str(s.get("error") or ""), "critic": str(s.get("critic") or ""), "editor": str(s.get("editor") or ""),
        "open": {"kind": "improve", "id": str(s.get("id") or "")},
    }


def run_row(r: Dict[str, Any]) -> Dict[str, Any]:
    err = str(r.get("error") or "")
    pr = _num(r.get("pass_rate"))
    ok = (not err) and (pr is None or pr >= 0.999)
    ts = str(r.get("ts") or "")
    return {
        "kind": "run", "id": str(r.get("run_id") or ""), "label": str(r.get("task") or r.get("label") or "run"),
        "template": "", "tag": "", "profile": str(r.get("profile") or ""),
        "ts": ts, "ended_at": _epoch(ts),
        "status": ("error" if err else ("done" if ok else "failed")), "live": False, "excluded": "",
        "tasks_n": 1, "done": 1 if ok else 0, "failed": 0 if ok else 1, "capped": 0,
        "pass_rate": pr, "avg_combined": _num(r.get("combined")),
        "quality_mean": None, "wall_total_s": _num(r.get("elapsed_s")),
        "code": None, "routing": None, "provenance": None,
        "warnings": 0, "reruns": 0,
        "source": str(r.get("source") or ""), "task": str(r.get("task") or ""), "run_id": str(r.get("run_id") or ""),
        "where": str(r.get("where") or ""), "variant": str(r.get("variant") or ""),
        "error": err[:200],
        "commits": [c for c in (r.get("commits") or []) if isinstance(c, dict)],
        "open": {"kind": "run", "id": str(r.get("run_id") or "")},
    }


def driver_rows(census_runs: Iterable[Dict[str, Any]], suites: Iterable[Dict[str, Any]],
                sessions: Iterable[Dict[str, Any]], runs: Iterable[Dict[str, Any]],
                *, live_census: bool = False) -> List[Dict[str, Any]]:
    """Every driver run as one row, newest first, the live one(s) first.

    A suite scoreboard the harness posted for a census run (source=census)
    is that census run and is dropped in favour of the archive's row; the
    row says `also_in_store` so the Runs/Suite readers are known to have it.
    A single run that belongs to a suite, session or census run is not a row.
    """
    rows: List[Dict[str, Any]] = []
    census_ids = set()
    for s in census_runs or []:
        if not isinstance(s, dict) or not s.get("run_id"):
            continue
        row = census_row(s, live=(live_census and s.get("run_id") == "current"))
        census_ids.add(row["id"])
        rows.append(row)
    by_id = {r["id"]: r for r in rows}
    for s in suites or []:
        if not isinstance(s, dict) or not s.get("suite_id"):
            continue
        if str(s.get("source") or "") == "census":
            base = str(s.get("census_run") or s.get("suite_id") or "")
            hit = by_id.get(base) or next((r for r in rows if r["kind"] == "census" and r["id"].startswith(base + "-")), None)
            if hit is not None:
                hit["also_in_store"] = True
                continue
            row = suite_row(s)
            row.update({"kind": "census", "label": base, "template": str(s.get("template") or ""),
                        "open": {"kind": "census", "id": base}, "excluded": str(s.get("excluded") or "")})
            rows.append(row)
            continue
        rows.append(suite_row(s))
    for s in sessions or []:
        if isinstance(s, dict) and s.get("id"):
            rows.append(improve_row(s))
    for r in runs or []:
        if not isinstance(r, dict) or not r.get("run_id"):
            continue
        if str(r.get("source") or "") in CHILD_SOURCES:
            continue
        rows.append(run_row(r))
    rows.sort(key=lambda r: (1 if r.get("live") else 0, r.get("ended_at") or 0.0, r.get("id") or ""), reverse=True)
    return rows


def filter_rows(rows: Sequence[Dict[str, Any]], *, kind: str = "", template: str = "", text: str = "",
                include_excluded: bool = True, source: str = "") -> List[Dict[str, Any]]:
    """The table's filters, server-side for callers that want fewer bytes."""
    q = str(text or "").strip().lower()
    out = []
    for r in rows:
        if kind and r.get("kind") != kind:
            continue
        if template and r.get("template") != template:
            continue
        if source and r.get("source") != source:
            continue
        if not include_excluded and r.get("excluded") and not r.get("live"):
            continue
        if q:
            hay = " ".join(str(r.get(k) or "") for k in ("id", "label", "template", "tag", "profile", "task", "source", "status")).lower()
            prov = r.get("provenance") or {}
            hay += " " + " ".join(str(prov.get(k) or "") for k in ("operator", "tool", "skill"))
            ro = r.get("routing") or {}
            hay += " " + " ".join(ro.get("coders") or []) + " " + " ".join((ro.get("nodes") or {}).keys())
            if q not in hay.lower():
                continue
        out.append(r)
    return out


def task_row(task: Dict[str, Any], overview: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A task definition with its history rolled in (evolve.tasks.overview):
    the tasks view of the Work table needs one shape too."""
    o = overview or {}
    st = o.get("stats") or {}
    c = task.get("census") if isinstance(task.get("census"), dict) else {}
    return {
        "id": str(task.get("id") or ""), "label": str(task.get("label") or ""),
        "type": str(task.get("type") or "loop"), "profile": str(task.get("profile") or ""),
        "cap": str(task.get("cap") or ""), "enabled": task.get("enabled") is not False,
        "tags": list(task.get("tags") or []), "checks_n": len(task.get("checks") or []),
        "template": str(c.get("template") or o.get("template") or ""),
        "goal": str(task.get("goal") or "")[:200],
        "seeded": bool(c), "overrides": task.get("overrides") if isinstance(task.get("overrides"), dict) else None,
        "runs": int(o.get("runs") or 0), "stats": st, "last": o.get("last"), "series": o.get("series") or [],
        "ok_rate": st.get("ok_rate"), "wall_median_s": st.get("wall_median_s"),
        "trend_ok_rate": st.get("trend_ok_rate"), "streak": st.get("streak"),
    }
