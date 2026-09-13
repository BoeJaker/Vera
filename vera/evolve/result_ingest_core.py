"""A census goal's row as a run record - the WRITE side of one result, many
drivers.

Slice 1 (task_history_core) read the census archive and the suite store and
gave every result one shape. This is the other half: the census harness posts
each finished goal to `evolve.result.ingest`, the goal becomes a run record in
the suite store (`vera:evolve:runs`, detail under `vera:evolve:run:<id>`), and
a finished census run becomes a suite scoreboard (`vera:evolve:suites`) tagged
`census-<template>` - the tag the seeded tasks already carry and the one
`evolve.suites(tag=...)` reads a template's timeline by. So evolve.runs,
evolve.suites, evolve.report, evolve.board, evolve.activity and the census
template card's own timeline see census results without knowing the archive
exists, and a census run is a driver run like a suite is.

The archive is still the baseline. The run store is a ROLLING WINDOW for every
driver (RUNS_CAP compact records, details for 14 days, SUITES_CAP scoreboards)
and that is deliberately unchanged: a task's long history is the archive plus
the index (evolve.task.history), which dedupes an ingested record against its
archive row on (task, run_id) and prefers the archive row. Raising the caps
for one source would put two retention regimes in one list, for a history the
archive already answers.

Pure: no I/O, no Redis. The capability in task_history_capabilities stores
what this builds.

    run record (compact)  the fields every evolve.runs consumer already reads
                          (run_id, task, ts, elapsed_s, pass_rate, checks_ok,
                          checks_n, combined, source, session, error, where)
                          plus the census facts the index needs (status,
                          hit_cap, wall_cap_s, code, routing, census_run,
                          loop_session)
    run record (detail)   the compact record + checks in the suite's shape +
                          the whole row under `census_row`
    suite scoreboard      {suite_id: <census run>, tag: census-<template>,
                           results: [...]} with source=census
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .task_history_core import census_task_id

SOURCE = "census"
#: The harness drives prod's OWN loop over HTTP - neither the dev sandbox the
#: suite would use nor the in-process fallback. Its own word, so the Runs view
#: never dresses a census goal up as either.
WHERE = "prod-loop"
#: The model string the harness writes when it left routing alone.
ROUTING_DEFAULT = "(routing default)"
#: The suite's own rule for a run with nothing to check: clean is 1.0.
_NO_CHECKS_OK, _NO_CHECKS_BAD = 1.0, 0.0


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").strip().lower()).strip("-")


def census_tag(template: str) -> str:
    """The tag census_seed puts on a template's tasks: census-<template>."""
    return "census-%s" % (_slug(template) or "unnamed")


def census_run_name(template: str, n: int) -> str:
    """The archive id census_all.sh will give this run: run<N> for the default
    series, <template>-run<N> for any other."""
    t = str(template or "default").strip() or "default"
    return "run%d" % int(n) if t == "default" else "%s-run%d" % (t, int(n))


def next_run_number(existing: Iterable[str], template: str) -> int:
    """Mirror of census_all.sh next_n: one past the highest number any archive
    of this series carries, markers included (`census.run51-failed-x.jsonl`
    still took 51). The default series began at 51 for the driver; the
    numbers before it were archived by hand."""
    t = str(template or "default").strip() or "default"
    pat = re.compile(r"^census\.run(\d+)" if t == "default" else r"^census\.%s-run(\d+)" % re.escape(t))
    ns = [int(m.group(1)) for name in existing for m in [pat.match(str(name or ""))] if m]
    return (max(ns) + 1) if ns else (51 if t == "default" else 1)


def run_id_for(row: Dict[str, Any], census_run: str) -> str:
    """The record id: the loop session, which is what the archive row's result
    is keyed by too (task_history_core.result_from_census_row) - the dedupe
    between an ingested record and its archive row rests on this rule being
    the same in both places. A goal that never got a session (abandoned,
    skipped) is keyed by run and goal."""
    sid = str((row or {}).get("session_id") or "").strip()
    return sid or "%s:%s" % (census_run, (row or {}).get("id"))


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _status(row: Dict[str, Any]) -> str:
    return str(row.get("status") or ("skipped" if row.get("skipped") else
                                     ("no-session" if row.get("no_session") else "unknown")))


def _code(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = row.get("code") if isinstance(row.get("code"), dict) else {}
    cv = row.get("code_version") if isinstance(row.get("code_version"), list) else []
    if not code.get("sha") and cv:
        sha, _, branch = str(cv[0]).partition("@")
        code = {"sha": sha, "sha_short": sha[:10], "branch": branch}
    if not code.get("sha"):
        return None
    return {"sha_short": str(code.get("sha_short") or code.get("sha") or "")[:10],
            "branch": str(code.get("branch") or ""),
            "changed": bool(code.get("changed_during_goal"))}


def _routing(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ro = row.get("routing") if isinstance(row.get("routing"), dict) else {}
    calls = ro.get("calls") if isinstance(ro.get("calls"), dict) else {}
    roles = ((ro.get("at_start") or {}).get("roles") or {}) if isinstance(ro.get("at_start"), dict) else {}
    if not calls:
        return None
    return {"coder": (roles.get("coder") or {}).get("model") or "",
            "nodes": calls.get("nodes") or {},
            "spill_calls": int(calls.get("spill_calls") or 0),
            "reroutes": len(calls.get("reroutes") or [])}


def checks_from_quality(quality: Any) -> List[Dict[str, Any]]:
    """The harness's quality results in the shape the run-detail modal renders
    (type / value / note / ok), so a census goal's checks read like a suite
    task's."""
    q = quality if isinstance(quality, dict) else {}
    out = []
    for c in q.get("results") or []:
        if not isinstance(c, dict):
            continue
        out.append({"type": "quality", "value": str(c.get("label") or c.get("kind") or "check"),
                    "note": str(c.get("detail") or ""), "ok": bool(c.get("ok"))})
    for f in q.get("files_missing") or []:
        out.append({"type": "file", "value": str(f), "note": "not produced", "ok": False})
    for f in q.get("files_found") or []:
        out.append({"type": "file", "value": str(f), "note": "", "ok": True})
    return out


def triggered_by(row: Dict[str, Any]) -> str:
    prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    op = str(prov.get("operator") or "").strip()
    return "census:%s" % op if op else "census"


def run_record_from_census_row(row: Dict[str, Any], *, template: str, census_run: str,
                               ts_fallback: str = "", goal_text: str = "", profile: str = "",
                               ingested_at: str = "") -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(compact, detail) for one census goal row, or None for a row that is
    not one. The compact record carries what the run list's readers expect
    plus the census facts; the detail carries the whole row."""
    if not isinstance(row, dict) or not row.get("id"):
        return None
    template = str(template or row.get("template") or "default").strip() or "default"
    census_run = str(census_run or "").strip()
    q = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    status = _status(row)
    ok = status == "done"
    checks_n = int(q.get("total") or 0)
    checks_ok = int(q.get("passed") or 0)
    if checks_n:
        pass_rate = round(checks_ok / checks_n, 3)
    else:
        pass_rate = _NO_CHECKS_OK if ok else _NO_CHECKS_BAD
    combined = round(pass_rate * 10, 1)
    wall = _num(row.get("wall_s")) or 0.0
    error = str(row.get("error") or "")
    if not error and not ok:
        # The suite's readers treat a non-empty error as "this run had a
        # problem"; a wall-capped or abandoned goal is one, and its status is
        # the most honest text for it.
        error = status
    ts = str(row.get("ended_at") or row.get("started_at") or ts_fallback or "")
    gid = str(row.get("id"))
    compact: Dict[str, Any] = {
        "run_id": run_id_for(row, census_run),
        "task": census_task_id(template, gid),
        "label": gid,
        "task_type": "loop",
        "profile": str(profile or ""),
        "ts": ts,
        "elapsed_s": round(float(wall), 1),
        "pass_rate": pass_rate,
        "checks_ok": checks_ok, "checks_n": checks_n,
        "score": None,
        "combined": combined,
        "variant": "",
        "source": SOURCE,
        "session": census_run,
        "loop_session": str(row.get("session_id") or ""),
        "error": error[:200],
        "where": WHERE,
        "triggered_by": triggered_by(row),
        # census facts the index and the Runs view read
        "status": status,
        "ok": ok,
        "hit_cap": status == "wall-cap",
        "wall_cap_s": _num(row.get("wall_cap_s")),
        "template": template,
        "goal_id": gid,
        "census_run": census_run,
        "code": _code(row),
        "routing": _routing(row),
        "reruns": len(row.get("reruns") or []),
        "planned": row.get("planned"), "executed": row.get("executed"),
        "tool_calls": row.get("tool_calls"),
        "warnings": len(row.get("warnings") or []),
        "model": str(row.get("model") or ""),
        "quality_score": _num(q.get("score")),
        "started_at": str(row.get("started_at") or ""),
        "ingested_at": str(ingested_at or ""),
    }
    detail = dict(compact)
    detail.update({
        "goal": str(goal_text or ""),
        "final": "",
        "steps": [],
        "checks": checks_from_quality(q),
        "raw_keys": [],
        "assessment": None,
        "blocked": False,
        "census_row": row,
    })
    return compact, detail


def suite_record_from_rows(rows: Sequence[Dict[str, Any]], *, template: str, census_run: str,
                           archive: str = "", excluded: str = "", profile: str = "",
                           ts_fallback: str = "", ingested_at: str = "") -> Optional[Dict[str, Any]]:
    """One census run as a suite scoreboard: the shape evolve.suites,
    evolve.report and evolve.board read, tagged census-<template>."""
    template = str(template or "default").strip() or "default"
    census_run = str(census_run or "").strip()
    results: List[Dict[str, Any]] = []
    ends: List[str] = []
    starts: List[str] = []
    models = set()
    codes = set()
    for row in rows or []:
        built = run_record_from_census_row(row, template=template, census_run=census_run,
                                           ts_fallback=ts_fallback, profile=profile)
        if not built:
            continue
        c, _ = built
        results.append({"task": c["task"], "label": c["label"], "type": "loop",
                        "profile": c["profile"], "run_id": c["run_id"],
                        "pass_rate": c["pass_rate"],
                        "checks": "%d/%d" % (c["checks_ok"], c["checks_n"]),
                        "elapsed_s": c["elapsed_s"], "error": c["error"],
                        "score": None, "combined": c["combined"],
                        "status": c["status"], "hit_cap": c["hit_cap"],
                        "loop_session": c["loop_session"]})
        if c["ts"]:
            ends.append(c["ts"])
        if c["started_at"]:
            starts.append(c["started_at"])
        if c["model"] and c["model"] != ROUTING_DEFAULT:
            models.add(c["model"])
        if c["code"]:
            codes.add("%s@%s" % (c["code"]["sha_short"], c["code"]["branch"]))
    if not results:
        return None
    combined = [r["combined"] for r in results if r.get("combined") is not None]
    return {
        "suite_id": census_run,
        "ts": (max(ends) if ends else str(ts_fallback or "")),
        "started_at": (min(starts) if starts else ""),
        "tasks_n": len(results),
        "avg_combined": round(statistics.mean(combined), 2) if combined else 0.0,
        "pass_rate": round(statistics.mean(r["pass_rate"] for r in results), 3),
        "critic": "",
        "variant": (sorted(models)[0] if len(models) == 1 else ""),
        "tag": census_tag(template),
        "profile": str(profile or ""),
        "results": results,
        "source": SOURCE,
        "template": template,
        "census_run": census_run,
        "archive": str(archive or ""),
        "excluded": str(excluded or ""),
        "done": sum(1 for r in results if r["status"] == "done"),
        "capped": sum(1 for r in results if r["hit_cap"]),
        "code_versions": sorted(codes),
        "ingested_at": str(ingested_at or ""),
    }


def compact_bytes(rec: Dict[str, Any]) -> int:
    """How much of the shared run list one record takes. The list is read whole
    by every runs view, so a record must stay small; the detail can be large."""
    return len(json.dumps(rec, default=str))
