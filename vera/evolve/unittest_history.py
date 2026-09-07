"""Every gate run, kept — so "race to green" has something to race across.

The Loop Lab has a "Gate race-to-green" strip. It reads `evolve.pipeline.list`,
filters to code pipelines, and draws one cell per PIPELINE coloured by
`gate_passed`. Two things follow, and both are what "it appears not to be
working" looks like from the outside:

  * `gate_passed` is a single scalar on the pipeline record, overwritten every
    time that pipeline is re-gated. A branch that went red, got fixed, and went
    green is ONE green cell. The race is exactly the thing the strip cannot show.
  * The gate already parses `{passed, failed, errors, skipped, rc}` on every
    run and then throws it away - it emits an event and returns. Nothing is
    stored, so there is no history of unit tests over time to display at all.

This module is the record. One row per RUN, not per pipeline.

WHY `total` IS A FIRST-CLASS FIELD
----------------------------------
A green run is not automatically a good run. These two are both "0 failed":

    2755 passed, 0 failed        <- 17 tests added since the last run
    2700 passed, 0 failed        <- 55 tests STOPPED RUNNING

The second is a test file that stopped being collected, a module dropped from
the critical tier, or an import error swallowed into a skip - and today it is
invisible, because the strip only knows green/red. `regressions()` names it.
That is the same failure this codebase has already been bitten by: a guard
"sat broken for days" because nothing ran it, and the gate cheerfully reported
PASS the whole time.

Pure: rows in, findings out. Storage and rendering live elsewhere.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: How many runs to keep. A run is a few hundred bytes and the gate fires a
#: handful of times a day, so this is months of history.
HISTORY_CAP = 500


def record(parsed: Dict[str, Any], *, branch: str = "", markers: str = "",
           paths: str = "tests", ts: str = "", pipeline_id: str = "",
           label: str = "") -> Dict[str, Any]:
    """One history row from a parsed pytest summary.

    `total` is stored rather than derived at read time: the counts are what the
    run actually reported, and recomputing them later from a changed formula
    would silently rewrite history.
    """
    def _n(key: str) -> int:
        try:
            return max(0, int(parsed.get(key) or 0))
        except (TypeError, ValueError):
            return 0
    passed, failed = _n("passed"), _n("failed")
    errors, skipped = _n("errors"), _n("skipped")
    return {
        "ts": str(ts or ""),
        "branch": str(branch or label or ""),
        "pipeline_id": str(pipeline_id or ""),
        "markers": str(markers or ""),
        "paths": str(paths or ""),
        "ok": bool(parsed.get("ok")),
        "passed": passed, "failed": failed, "errors": errors, "skipped": skipped,
        "total": passed + failed + errors + skipped,
        "rc": parsed.get("rc"),
        "summary": str(parsed.get("summary") or ""),
    }


def _rows(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [r for r in (rows or []) if isinstance(r, dict)]


def newest_first(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Sorted by timestamp, newest first. Rows with no ts sort last rather than
    crashing — a row missing its stamp is still evidence."""
    return sorted(_rows(rows), key=lambda r: str(r.get("ts") or ""), reverse=True)


def green_streak(rows: Optional[Iterable[Dict[str, Any]]]) -> int:
    """Consecutive green runs at the newest end. 0 while red."""
    n = 0
    for r in newest_first(rows):
        if not r.get("ok"):
            break
        n += 1
    return n


def race_to_green(rows: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, Any]:
    """The most recent red→green transition: how many runs it took, and when.

    This is the thing the strip is named after and could never show, because a
    pipeline only ever kept its LAST gate result.
    """
    ordered = list(reversed(newest_first(rows)))          # oldest first
    start: Optional[Dict[str, Any]] = None
    attempts = 0
    best: Optional[Dict[str, Any]] = None
    for r in ordered:
        if not r.get("ok"):
            if start is None:
                start = r
                attempts = 0
            attempts += 1
        elif start is not None:
            best = {"went_red_at": start.get("ts", ""), "went_green_at": r.get("ts", ""),
                    "red_runs": attempts, "branch": r.get("branch", "")}
            start, attempts = None, 0
    if start is not None:
        return {"still_red": True, "went_red_at": start.get("ts", ""),
                "red_runs": attempts, "branch": start.get("branch", "")}
    return best or {}


def trend(rows: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, Any]:
    """Oldest vs newest, so the test count over time is a number and not a vibe."""
    ordered = newest_first(rows)
    if not ordered:
        return {}
    new, old = ordered[0], ordered[-1]
    return {
        "runs": len(ordered),
        "from_ts": old.get("ts", ""), "to_ts": new.get("ts", ""),
        "passed": new.get("passed", 0), "passed_delta": new.get("passed", 0) - old.get("passed", 0),
        "total": new.get("total", 0), "total_delta": new.get("total", 0) - old.get("total", 0),
        "failed": new.get("failed", 0),
        "green": bool(new.get("ok")), "green_streak": green_streak(ordered),
    }


def regressions(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Runs where tests DISAPPEARED without failing.

    A green run with fewer tests than the run before it is not a pass — it is
    coverage that stopped being collected, and the gate reports PASS throughout.
    Only compared against the previous run on the SAME markers/paths, because
    the full suite and the critical tier legitimately have different totals.
    """
    out: List[Dict[str, Any]] = []
    by_scope: Dict[str, Dict[str, Any]] = {}
    for r in reversed(newest_first(rows)):                # oldest first
        scope = "%s|%s" % (r.get("markers", ""), r.get("paths", ""))
        prev = by_scope.get(scope)
        if prev is not None:
            lost = int(prev.get("total", 0)) - int(r.get("total", 0))
            if lost > 0 and not int(r.get("failed", 0)) and not int(r.get("errors", 0)):
                out.append({"ts": r.get("ts", ""), "branch": r.get("branch", ""),
                            "lost": lost, "was": prev.get("total", 0),
                            "now": r.get("total", 0), "markers": r.get("markers", ""),
                            "note": "%d test(s) stopped running — green, but on less"
                                    % lost})
        by_scope[scope] = r
    return out


def lanes(rows: Optional[Iterable[Dict[str, Any]]], limit: int = 40) -> List[Dict[str, Any]]:
    """The strip, oldest→newest: one cell per RUN.

    `delta` is against the previous cell so a hover can say "+17 tests" rather
    than only "green".
    """
    ordered = list(reversed(newest_first(rows)))[-max(1, int(limit or 40)):]
    out: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    for r in ordered:
        out.append({
            "ts": r.get("ts", ""), "branch": r.get("branch", ""),
            "ok": bool(r.get("ok")), "passed": r.get("passed", 0),
            "failed": r.get("failed", 0), "total": r.get("total", 0),
            "markers": r.get("markers", ""),
            "pipeline_id": r.get("pipeline_id", ""),
            "delta": (int(r.get("total", 0)) - int(prev.get("total", 0))) if prev else 0,
            "summary": r.get("summary", ""),
        })
        prev = r
    return out
