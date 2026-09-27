"""One picture of automated development work — whoever did it.

Loop Lab draws the same few pictures over and over: a status matrix (lanes of
runs, coloured by outcome), a race to green (how many red runs a lane took to
come good, and whether it has yet), the tests behind those runs, a board of the
work, one run's track through its stages, and two runs side by side. They were
each computed in the browser, from whichever list the view happened to fetch,
with whatever slice it happened to take — so the matrix kept 8 columns, the
race kept 40 cells, the sparkline read the OLDEST 24 results, and a run you
clicked lost the run you clicked.

This module computes those pictures once, from plain rows, into ONE payload
shape (``kind: "ci"``) that every surface draws: the Loop Lab page, the chat
canvas widgets, and any agent that asks. It never truncates on its own: a
caller that wants fewer columns asks for them, and the payload then says how
many it left out (``hidden``), so a picture never looks complete when it isn't.

The same builders serve three sources, because they are three views of one
thing — work that is tried, checked, and retried until it is green:

  * gate runs    (``evolve.unittest.history`` rows: a branch's pytest runs)
  * task runs    (Loop Lab task results: a task's scored attempts)
  * loop traces  (``workshop.agent_loop.trace``: a loop's steps, calls, gates)

Pure: rows in, payloads out. No I/O, no clock except what the rows carry.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

KIND = "ci"

#: controllers we can name. Everything else is shown under its own name.
_CONTROLLERS = (
    ("claude", ("claude", "claude_code", "claude-code")),
    ("codex", ("codex", "openai_codex")),
    ("vera", ("vera", "loop", "autonomous", "census", "dream", "improve", "agent_loop",
              "scheduler", "idle")),
    ("user", ("user", "human", "ui", "browser")),
)


# ─────────────────────────────────────────────────────────────────────────────
# small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _rows(rows: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    return [r for r in (rows or []) if isinstance(r, dict)]


def parse_ts(ts: Any) -> Optional[datetime]:
    """An ISO stamp (with or without Z / offset) → aware datetime, else None."""
    s = _s(ts).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def seconds_between(a: Any, b: Any) -> Optional[float]:
    da, db = parse_ts(a), parse_ts(b)
    if da is None or db is None:
        return None
    return round((db - da).total_seconds(), 1)


def controller_of(*values: Any) -> str:
    """The agent behind a run, named one way: claude · codex · vera · user.

    Pipelines say ``claude_code``, rows say ``claude``, a census says
    ``census`` — one lane per agent needs one spelling. The first value that
    names anything wins; an unknown non-empty value is kept as-is so a new
    agent shows up under its own name instead of vanishing into "other".
    """
    for v in values:
        k = _s(v).strip().lower()
        if not k:
            continue
        for name, aliases in _CONTROLLERS:
            if k in aliases or k.startswith(name):
                return name
        return k
    return ""


def outcome(row: Dict[str, Any]) -> str:
    """pass · fail · error · running · skip · unknown — one word per cell."""
    st = _s(row.get("status") or row.get("state")).lower()
    if st in ("running", "live", "queued", "pending", "drafting", "gating", "testing"):
        return "running"
    if row.get("ok") is True or st in ("pass", "passed", "ok", "green", "done", "merged",
                                       "promoted", "success"):
        return "pass"
    if _i(row.get("errors")) and not _i(row.get("failed")):
        return "error"
    if row.get("ok") is False or st in ("fail", "failed", "red", "error", "rolled_back",
                                        "rejected", "timeout", "cancelled"):
        return "fail"
    if st in ("skip", "skipped"):
        return "skip"
    return "unknown"


def _is_green(o: str) -> bool:
    return o == "pass"


def _is_red(o: str) -> bool:
    return o in ("fail", "error")


# ─────────────────────────────────────────────────────────────────────────────
# filtering — the same filter bar for every picture
# ─────────────────────────────────────────────────────────────────────────────

def filter_rows(rows: Optional[Iterable[Dict[str, Any]]], *, branch: str = "",
                q: str = "", controller: str = "", status: str = "",
                since: str = "", until: str = "", markers: str = "",
                repo: str = "", session_id: str = "") -> List[Dict[str, Any]]:
    """Rows matching every filter given. Blank filters match everything.

    ``branch`` is an exact match, or a prefix when it ends in ``*``/``/``
    (``feat/`` → every feature branch). ``q`` is a case-insensitive substring
    over branch, summary, pipeline id and failing test ids. ``status`` is an
    outcome word (pass, fail, error, running) or ``red`` (fail|error).
    """
    out: List[Dict[str, Any]] = []
    ql = q.strip().lower()
    ctl = controller_of(controller) if controller else ""
    st = status.strip().lower()
    lo, hi = parse_ts(since), parse_ts(until)
    br = branch.strip()
    for r in _rows(rows):
        b = _s(r.get("branch"))
        if br:
            if br.endswith("*"):
                if not b.startswith(br[:-1]):
                    continue
            elif br.endswith("/"):
                if not b.startswith(br):
                    continue
            elif b != br:
                continue
        if markers and _s(r.get("markers")) != markers:
            continue
        if repo and _s(r.get("repo") or "vera") != repo:
            continue
        if session_id and _s(r.get("session_id")) != session_id:
            continue
        if ctl and controller_of(r.get("controller"), r.get("via")) != ctl:
            continue
        if st:
            o = outcome(r)
            if st == "red":
                if not _is_red(o):
                    continue
            elif st in ("green", "ok"):
                if o != "pass":
                    continue
            elif o != st:
                continue
        if lo or hi:
            t = parse_ts(r.get("ts") or r.get("created_at"))
            if t is None or (lo and t < lo) or (hi and t > hi):
                continue
        if ql:
            hay = " ".join([b, _s(r.get("summary")), _s(r.get("pipeline_id") or r.get("id")),
                            _s(r.get("title")), _s(r.get("task")),
                            " ".join(_s(f.get("node_id")) for f in _rows(r.get("failures")))])
            if ql not in hay.lower():
                continue
        out.append(r)
    return out


def oldest_first(rows: Optional[Iterable[Dict[str, Any]]], key: str = "ts") -> List[Dict[str, Any]]:
    return sorted(_rows(rows), key=lambda r: _s(r.get(key) or r.get("created_at")))


# ─────────────────────────────────────────────────────────────────────────────
# cells, lanes, races
# ─────────────────────────────────────────────────────────────────────────────

def cell_of(row: Dict[str, Any], prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One run as a matrix cell. Carries everything a hover or a drill needs —
    nothing is thrown away to make the cell small; the renderer decides."""
    total = _i(row.get("total"))
    passed = _i(row.get("passed"))
    c = {
        "id": _s(row.get("run_id") or row.get("id") or row.get("pipeline_id") or row.get("ts")),
        "ts": _s(row.get("ts") or row.get("created_at")),
        "o": outcome(row),
        "passed": passed, "failed": _i(row.get("failed")), "errors": _i(row.get("errors")),
        "skipped": _i(row.get("skipped")), "total": total,
        "pipeline_id": _s(row.get("pipeline_id")),
        "controller": controller_of(row.get("controller"), row.get("via")),
        "session_id": _s(row.get("session_id")),
        "summary": _s(row.get("summary")),
    }
    if row.get("score") is not None:
        try:
            c["score"] = round(float(row.get("score")), 3)
        except (TypeError, ValueError):
            pass
    if row.get("ms") is not None:
        c["ms"] = _i(row.get("ms"))
    if row.get("label"):
        c["label"] = _s(row.get("label"))
    fails = _rows(row.get("failures"))
    if fails:
        c["failing"] = [_s(f.get("node_id") or f.get("name")) for f in fails]
    if prev is not None:
        c["delta"] = total - _i(prev.get("total"))
    return c


def race_of(cells: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A lane's race to green, from its cells oldest→newest.

    state:
      green   — newest run passed (``streak`` green runs in a row)
      red     — newest run is red and has never been green since it went red
      racing  — red now, but a run is in flight
      none    — no decided run yet
    Every completed red→green stretch is kept in ``laps`` (runs it took, and
    how long) so the chart can show the race each time, not only the last.
    """
    laps: List[Dict[str, Any]] = []
    start: Optional[Dict[str, Any]] = None
    reds = 0
    for c in cells:
        o = c.get("o")
        if _is_red(o):
            if start is None:
                start, reds = c, 0
            reds += 1
        elif _is_green(o) and start is not None:
            laps.append({"went_red_at": start.get("ts"), "went_green_at": c.get("ts"),
                         "red_runs": reds, "attempts": reds + 1,
                         "seconds": seconds_between(start.get("ts"), c.get("ts")),
                         "from": start.get("id"), "to": c.get("id")})
            start, reds = None, 0
    decided = [c for c in cells if c.get("o") in ("pass", "fail", "error")]
    last = decided[-1] if decided else None
    running = bool(cells) and cells[-1].get("o") == "running"
    streak = 0
    for c in reversed(decided):
        if not _is_green(c.get("o")):
            break
        streak += 1
    if last is None:
        state = "racing" if running else "none"
    elif _is_green(last.get("o")):
        state = "green"
    else:
        state = "racing" if running else "red"
    out: Dict[str, Any] = {"state": state, "streak": streak, "laps": laps,
                           "runs": len(cells)}
    if start is not None:
        out.update({"red_since": start.get("ts"), "red_runs": reds})
    if laps:
        out["last_lap"] = laps[-1]
        secs = [lp["seconds"] for lp in laps if lp.get("seconds") is not None]
        out["best_attempts"] = min(lp["attempts"] for lp in laps)
        if secs:
            out["best_seconds"] = min(secs)
    return out


def _lane_key(row: Dict[str, Any], group: str) -> str:
    if group == "controller":
        return controller_of(row.get("controller"), row.get("via")) or "unattributed"
    if group == "markers":
        return _s(row.get("markers")) or "all"
    if group == "day":
        return _s(row.get("ts") or row.get("created_at"))[:10] or "undated"
    if group == "task":
        return _s(row.get("task") or row.get("task_id")) or "untitled"
    return _s(row.get(group) or row.get("branch") or row.get("label")) or "unnamed"


def lanes_of(rows: Optional[Iterable[Dict[str, Any]]], *, group: str = "branch",
             cols: int = 0, order: str = "recent") -> List[Dict[str, Any]]:
    """Group rows into lanes of cells (oldest→newest), each with its race.

    ``cols`` > 0 keeps the newest N cells per lane and records how many older
    ones it left out as ``hidden`` — a lane is never silently short. ``order``:
    recent (lane with the newest run first) · red (red lanes first, then
    recent) · name.
    """
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in oldest_first(rows):
        by.setdefault(_lane_key(r, group), []).append(r)
    lanes: List[Dict[str, Any]] = []
    for key, rs in by.items():
        cells: List[Dict[str, Any]] = []
        prev = None
        for r in rs:
            cells.append(cell_of(r, prev))
            prev = r
        race = race_of(cells)
        hidden = 0
        if cols and len(cells) > cols:
            hidden = len(cells) - cols
            cells = cells[-cols:]
        ctls = sorted({c["controller"] for c in cells if c.get("controller")})
        last = cells[-1] if cells else {}
        lanes.append({"id": key, "name": key, "cells": cells, "hidden": hidden,
                      "race": race, "state": race["state"], "controllers": ctls,
                      "last_ts": last.get("ts", ""), "total": last.get("total", 0),
                      "passed": last.get("passed", 0), "failed": last.get("failed", 0),
                      "pipeline_id": next((c.get("pipeline_id") for c in reversed(cells)
                                           if c.get("pipeline_id")), "")})
    rank = {"red": 0, "racing": 1, "none": 2, "green": 3}
    if order == "red":
        lanes.sort(key=lambda ln: (rank.get(ln["state"], 9), _neg(ln["last_ts"])))
    elif order == "name":
        lanes.sort(key=lambda ln: ln["name"])
    else:
        lanes.sort(key=lambda ln: _neg(ln["last_ts"]))
    return lanes


def _neg(ts: str) -> str:
    # sort newest first with an ascending sort: invert each character
    return "".join(chr(0x10FFFF - ord(ch)) for ch in (ts or ""))


def summary_of(lanes: Sequence[Dict[str, Any]], rows: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The headline numbers over lanes (and optionally the raw rows)."""
    states: Dict[str, int] = {}
    for ln in lanes:
        states[ln["state"]] = states.get(ln["state"], 0) + 1
    cells = [c for ln in lanes for c in ln["cells"]]
    decided = [c for c in cells if c["o"] in ("pass", "fail", "error")]
    green = sum(1 for c in decided if c["o"] == "pass")
    ctl: Dict[str, Dict[str, int]] = {}
    for c in cells:
        k = c.get("controller") or "unattributed"
        e = ctl.setdefault(k, {"runs": 0, "pass": 0, "red": 0})
        e["runs"] += 1
        if c["o"] == "pass":
            e["pass"] += 1
        elif _is_red(c["o"]):
            e["red"] += 1
    laps = [lp for ln in lanes for lp in ln["race"].get("laps", [])]
    secs = sorted(lp["seconds"] for lp in laps if lp.get("seconds") is not None)
    out = {"lanes": len(lanes), "runs": len(cells) + sum(ln.get("hidden", 0) for ln in lanes),
           "shown": len(cells), "green": green, "red": len(decided) - green,
           "running": sum(1 for c in cells if c["o"] == "running"),
           "pass_rate": round(green / len(decided), 3) if decided else None,
           "states": states, "controllers": ctl,
           "laps": len(laps),
           "median_attempts": _median([lp["attempts"] for lp in laps]),
           "median_seconds_to_green": _median(secs)}
    newest = max((c["ts"] for c in cells if c.get("ts")), default="")
    if newest:
        out["newest"] = newest
    return out


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else round((xs[m - 1] + xs[m]) / 2, 1)


# ─────────────────────────────────────────────────────────────────────────────
# the pictures
# ─────────────────────────────────────────────────────────────────────────────

def matrix(rows: Optional[Iterable[Dict[str, Any]]], *, group: str = "branch",
           cols: int = 0, order: str = "recent", title: str = "",
           source: str = "") -> Dict[str, Any]:
    """Status matrix: one lane per group, one cell per run."""
    lanes = lanes_of(rows, group=group, cols=cols, order=order)
    return {"kind": KIND, "view": "matrix", "title": title or "Status matrix",
            "group": group, "source": source, "lanes": lanes,
            "summary": summary_of(lanes)}


def race(rows: Optional[Iterable[Dict[str, Any]]], *, group: str = "branch",
         cols: int = 0, title: str = "", source: str = "") -> Dict[str, Any]:
    """Race to green: the same lanes, ordered as a race — red lanes still
    running first, then lanes by how quickly they last came good."""
    lanes = lanes_of(rows, group=group, cols=cols, order="recent")
    rank = {"racing": 0, "red": 1, "none": 2, "green": 3}

    def key(ln: Dict[str, Any]):
        lap = ln["race"].get("last_lap") or {}
        return (rank.get(ln["state"], 9), lap.get("attempts", 0), _neg(ln["last_ts"]))
    lanes.sort(key=key)
    return {"kind": KIND, "view": "race", "title": title or "Race to green",
            "group": group, "source": source, "lanes": lanes,
            "summary": summary_of(lanes)}


def test_grid(rows: Optional[Iterable[Dict[str, Any]]], *, cols: int = 0,
              title: str = "", source: str = "") -> Dict[str, Any]:
    """Every test that failed in any run, against every run (oldest→newest).

    A cell is ``fail``/``error`` when the run named that test as failing,
    ``pass`` when the run completed with a full failure list that did not name
    it, ``unknown`` when the run's failure list was cut short (so absence
    proves nothing) or the run recorded no failure list at all, and ``-``
    before the test first appeared. Each test is classified:

      broken  — failing in the newest run
      fixed   — failed before, passing in the newest run
      flaky   — flipped pass↔fail at least twice
    """
    runs = oldest_first(rows)
    seen_at: Dict[str, int] = {}
    for i, r in enumerate(runs):
        for f in _rows(r.get("failures")):
            nid = _s(f.get("node_id") or f.get("name"))
            if nid and nid not in seen_at:
                seen_at[nid] = i
    hidden = 0
    if cols and len(runs) > cols:
        hidden = len(runs) - cols
    col_runs = runs[hidden:]
    columns = [{"id": _s(r.get("pipeline_id") or r.get("ts")), "ts": _s(r.get("ts")),
                "branch": _s(r.get("branch")), "o": outcome(r),
                "controller": controller_of(r.get("controller"), r.get("via")),
                "pipeline_id": _s(r.get("pipeline_id")),
                "failed": _i(r.get("failed")) + _i(r.get("errors")), "total": _i(r.get("total"))}
               for r in col_runs]
    tests: List[Dict[str, Any]] = []
    for nid, first in seen_at.items():
        cells: List[str] = []
        desc = ""
        for i, r in enumerate(runs):
            if i < first:
                c = "-"
            else:
                hit = next((f for f in _rows(r.get("failures"))
                            if _s(f.get("node_id") or f.get("name")) == nid), None)
                if hit is not None:
                    c = "error" if _s(hit.get("kind")) == "error" else "fail"
                    desc = _s(hit.get("description")) or desc
                elif "failures" not in r or r.get("failures_truncated"):
                    c = "unknown"
                elif outcome(r) in ("pass", "fail", "error"):
                    c = "pass"
                else:
                    c = "unknown"
            cells.append(c)
        known = [c for c in cells if c in ("pass", "fail", "error")]
        flips = sum(1 for a, b in zip(known, known[1:]) if (a == "pass") != (b == "pass"))
        latest = next((c for c in reversed(cells) if c in ("pass", "fail", "error")), "")
        cls = "broken" if latest in ("fail", "error") else ("fixed" if latest == "pass" else "unknown")
        if flips >= 2:
            cls = "flaky"
        tests.append({"id": nid, "name": nid.split("::")[-1], "module": nid.split("::")[0],
                      "cells": cells[hidden:], "fails": sum(1 for c in cells if c in ("fail", "error")),
                      "flips": flips, "class": cls, "description": desc,
                      "first_seen": _s(runs[first].get("ts"))})
    order = {"broken": 0, "flaky": 1, "unknown": 2, "fixed": 3}
    tests.sort(key=lambda t: (order.get(t["class"], 9), -t["fails"], t["id"]))
    counts: Dict[str, int] = {}
    for t in tests:
        counts[t["class"]] = counts.get(t["class"], 0) + 1
    return {"kind": KIND, "view": "tests", "title": title or "Tests across runs",
            "source": source, "columns": columns, "tests": tests, "hidden": hidden,
            "summary": {"tests": len(tests), "runs": len(runs), "shown": len(col_runs),
                        "classes": counts,
                        # runs that recorded WHICH tests failed (older history, and an instance still on the old
                        # writer, record counts only - an empty grid over those proves nothing)
                        "listed": sum(1 for r in runs if "failures" in r),
                        "red_runs": sum(1 for r in runs if outcome(r) in ("fail", "error"))}}


def compare(a: Dict[str, Any], b: Dict[str, Any], *, title: str = "") -> Dict[str, Any]:
    """Run ``a`` (before) against run ``b`` (after): which tests it fixed,
    broke, or left failing, and how the counts moved."""
    fa = {_s(f.get("node_id") or f.get("name")): f for f in _rows((a or {}).get("failures"))}
    fb = {_s(f.get("node_id") or f.get("name")): f for f in _rows((b or {}).get("failures"))}
    fixed = sorted(k for k in fa if k not in fb)
    broken = sorted(k for k in fb if k not in fa)
    still = sorted(k for k in fb if k in fa)

    def side(r: Dict[str, Any]) -> Dict[str, Any]:
        r = r or {}
        return {"id": _s(r.get("pipeline_id") or r.get("id") or r.get("ts")), "ts": _s(r.get("ts")),
                "branch": _s(r.get("branch")), "o": outcome(r),
                "controller": controller_of(r.get("controller"), r.get("via")),
                "passed": _i(r.get("passed")), "failed": _i(r.get("failed")),
                "errors": _i(r.get("errors")), "skipped": _i(r.get("skipped")),
                "total": _i(r.get("total")), "summary": _s(r.get("summary"))}
    sa, sb = side(a), side(b)
    trunc = bool((a or {}).get("failures_truncated") or (b or {}).get("failures_truncated"))
    return {"kind": KIND, "view": "compare", "title": title or "Compare runs",
            "a": sa, "b": sb,
            "delta": {k: sb[k] - sa[k] for k in ("passed", "failed", "errors", "skipped", "total")},
            "seconds": seconds_between(sa["ts"], sb["ts"]),
            "fixed": [{"id": k, "description": _s(fa[k].get("description"))} for k in fixed],
            "broken": [{"id": k, "description": _s(fb[k].get("description"))} for k in broken],
            "still": [{"id": k, "description": _s(fb[k].get("description"))} for k in still],
            "partial": trunc}


def pulse(rows: Optional[Iterable[Dict[str, Any]]], *, buckets: str = "day",
          title: str = "", source: str = "") -> Dict[str, Any]:
    """The headline strip: pass rate and run count per day (or hour), the
    agents behind them, and the current streak."""
    rs = oldest_first(rows)
    width = 13 if buckets == "hour" else 10
    by: Dict[str, Dict[str, Any]] = {}
    for r in rs:
        k = _s(r.get("ts") or r.get("created_at"))[:width]
        if not k:
            continue
        e = by.setdefault(k, {"t": k, "runs": 0, "pass": 0, "red": 0, "tests": 0})
        o = outcome(r)
        e["runs"] += 1
        e["pass"] += 1 if o == "pass" else 0
        e["red"] += 1 if _is_red(o) else 0
        e["tests"] = max(e["tests"], _i(r.get("total")))
    series = []
    for k in sorted(by):
        e = by[k]
        d = e["pass"] + e["red"]
        e["rate"] = round(e["pass"] / d, 3) if d else None
        series.append(e)
    lanes = lanes_of(rs)
    return {"kind": KIND, "view": "pulse", "title": title or "CI pulse", "source": source,
            "series": series, "summary": summary_of(lanes),
            "latest": cell_of(rs[-1]) if rs else {}}


# ─────────────────────────────────────────────────────────────────────────────
# a pipeline's track, a board, the fleet
# ─────────────────────────────────────────────────────────────────────────────

#: the stages a pipeline passes through, in order; step names map onto them
TRACK = ("begin", "commit", "compile", "tests", "review", "promote")
_STAGE_OF = {
    "begin": "begin", "adopt": "begin", "draft": "begin", "branch": "begin",
    "commit": "commit", "commits": "commit", "edit": "commit", "apply": "commit",
    "compile": "compile", "gate": "compile", "syntax": "compile",
    "critical-tests": "tests", "tests": "tests", "unittest": "tests", "test": "tests",
    "review": "review", "review_request": "review", "critic": "review",
    "promote": "promote", "merge": "promote", "rollback": "promote",
}


def track(pipeline: Dict[str, Any]) -> Dict[str, Any]:
    """One pipeline as a track of stages, every step's FULL detail kept."""
    p = pipeline or {}
    steps = _rows(p.get("steps"))
    stages: Dict[str, Dict[str, Any]] = {s: {"name": s, "status": "todo", "steps": []} for s in TRACK}
    for st in steps:
        name = _s(st.get("stage")).lower()
        key = _STAGE_OF.get(name) or next((v for k, v in _STAGE_OF.items() if k in name), "")
        if not key:
            key = "commit"
        slot = stages[key]
        slot["steps"].append({"stage": _s(st.get("stage")), "ok": st.get("ok"),
                              "detail": _s(st.get("detail")), "ts": _s(st.get("ts"))})
        slot["status"] = "done" if st.get("ok") else "failed"
        slot["ts"] = _s(st.get("ts"))
    dec = _s(p.get("decision")).lower()
    if dec in ("promoted", "merged"):
        stages["promote"]["status"] = "done"
    elif dec in ("rolled_back", "rejected"):
        stages["promote"]["status"] = "failed"
    if p.get("review_requested") and stages["review"]["status"] == "todo":
        stages["review"]["status"] = "now"
    if p.get("gate_passed") is True and stages["tests"]["status"] == "todo":
        stages["tests"]["status"] = "done"
    if p.get("gate_passed") is False and stages["tests"]["status"] == "todo":
        stages["tests"]["status"] = "failed"
    ordered = [stages[s] for s in TRACK]
    if _s(p.get("status")).lower() in ("running", "gating", "testing", "drafting") or p.get("live"):
        nxt = next((s for s in ordered if s["status"] == "todo"), None)
        if nxt:
            nxt["status"] = "now"
    return {"kind": KIND, "view": "track", "title": _s(p.get("branch")) or _s(p.get("id")),
            "pipeline": {k: p.get(k) for k in ("id", "branch", "status", "decision", "gate_passed",
                                                "created_at", "ended_at", "session_id", "to",
                                                "repo", "review_requested")},
            "controller": controller_of(p.get("controller"), p.get("via")),
            "stages": ordered, "commits": p.get("commits") or [],
            "changed_files": p.get("changed_files") or []}


#: board lanes in reading order
BOARD_LANES = ("inbox", "ready", "queued_vera", "in_progress", "in_progress_vera", "blocked",
               "needs_review", "review", "done", "dropped")


def board(items: Optional[Iterable[Dict[str, Any]]], pipelines: Optional[Iterable[Dict[str, Any]]] = None,
          *, title: str = "", include_done: bool = True) -> Dict[str, Any]:
    """Board items as columns, each card joined to its pipeline's gate state."""
    pipes = {_s(p.get("id")): p for p in _rows(pipelines)}
    by_branch = {}
    for p in _rows(pipelines):
        by_branch.setdefault(_s(p.get("branch")), p)
    cols: Dict[str, List[Dict[str, Any]]] = {}
    for it in _rows(items):
        lane = _s(it.get("lane")) or "inbox"
        if not include_done and lane in ("done", "dropped"):
            continue
        p = pipes.get(_s(it.get("pipeline"))) or by_branch.get(_s(it.get("branch"))) or {}
        cols.setdefault(lane, []).append({
            "id": _s(it.get("id")), "title": _s(it.get("title")), "lane": lane,
            "agent": controller_of(it.get("agent")) or _s(it.get("agent")),
            "branch": _s(it.get("branch")), "pipeline": _s(it.get("pipeline") or p.get("id")),
            "session": _s(it.get("session")), "labels": it.get("labels") or [],
            "comments": _i(it.get("comment_count")),
            "gate": ("pass" if p.get("gate_passed") is True else
                     "fail" if p.get("gate_passed") is False else ""),
            "decision": _s(p.get("decision")),
            "updated_at": _s(it.get("updated_at") or it.get("created_at")),
        })
    order = [ln for ln in BOARD_LANES if ln in cols] + sorted(k for k in cols if k not in BOARD_LANES)
    columns = [{"name": ln, "items": sorted(cols[ln], key=lambda c: _neg(c["updated_at"]))} for ln in order]
    return {"kind": KIND, "view": "board", "title": title or "Board", "columns": columns,
            "summary": {"items": sum(len(c["items"]) for c in columns),
                        "by_lane": {c["name"]: len(c["items"]) for c in columns}}}


def fleet(sandboxes: Optional[Iterable[Dict[str, Any]]], pipelines: Optional[Iterable[Dict[str, Any]]] = None,
          sessions: Optional[Iterable[Dict[str, Any]]] = None, *, title: str = "") -> Dict[str, Any]:
    """Who is working where: each sandbox with its branch's latest pipeline and
    the conversations that drove it."""
    by_branch: Dict[str, Dict[str, Any]] = {}
    for p in _rows(pipelines):
        by_branch.setdefault(_s(p.get("branch")), p)
    convs: Dict[str, List[Dict[str, Any]]] = {}
    for s in _rows(sessions):
        convs.setdefault(_s(s.get("branch")), []).append(s)
    cards = []
    for sb in _rows(sandboxes):
        b = _s(sb.get("branch"))
        p = by_branch.get(b, {})
        cards.append({"name": _s(sb.get("name")), "branch": b, "role": _s(sb.get("role")),
                      "running": bool(sb.get("running")), "pinned": bool(sb.get("pinned")),
                      "port": sb.get("port"), "url": _s(sb.get("url")),
                      "owner": controller_of(sb.get("owner")) or _s(sb.get("owner")),
                      "last_activity": _s(sb.get("last_activity")),
                      "pipeline": _s(p.get("id")), "status": _s(p.get("status")),
                      "gate": ("pass" if p.get("gate_passed") is True else
                               "fail" if p.get("gate_passed") is False else ""),
                      "decision": _s(p.get("decision")),
                      "conversations": len(convs.get(b, []))})
    cards.sort(key=lambda c: (not c["running"], _neg(c["last_activity"])))
    return {"kind": KIND, "view": "fleet", "title": title or "Fleet", "cards": cards,
            "summary": {"sandboxes": len(cards), "running": sum(1 for c in cards if c["running"]),
                        "red": sum(1 for c in cards if c["gate"] == "fail")}}


# ─────────────────────────────────────────────────────────────────────────────
# the agentic loop — the same pictures from one loop's trace
# ─────────────────────────────────────────────────────────────────────────────

def _loop_steps(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Plan steps joined to what executed, de-duplicated by step id."""
    t = trace or {}
    plan = _rows(((t.get("plan") or {}).get("steps")))
    done: Dict[str, Dict[str, Any]] = {}
    for st in _rows(t.get("steps")):
        done[_s(st.get("step_id"))] = st
    ids = [_s(p.get("id")) for p in plan] + [k for k in done if k not in {_s(p.get("id")) for p in plan}]
    out = []
    pmap = {_s(p.get("id")): p for p in plan}
    for sid in ids:
        p, e = pmap.get(sid, {}), done.get(sid, {})
        calls = _rows(e.get("calls"))
        out.append({"id": sid, "title": _s(e.get("title") or p.get("title")) or f"step {sid}",
                    "calls": calls, "ok": e.get("ok"), "executed": bool(e),
                    "success": _s(p.get("success"))})
    return out


def loop_matrix(trace: Dict[str, Any], *, title: str = "") -> Dict[str, Any]:
    """A loop as a status matrix: one lane per step, one cell per tool call."""
    t = trace or {}
    lanes = []
    for st in _loop_steps(t):
        cells = []
        for c in st["calls"]:
            ok = c.get("ok")
            cells.append({"id": f"{st['id']}.{_i(c.get('cycle'))}", "ts": "",
                          "o": "pass" if ok else ("fail" if ok is False else "unknown"),
                          "label": _s(c.get("tool")), "ms": _i(c.get("ms")),
                          "summary": _s(c.get("args")), "repeat": bool(c.get("repeat")),
                          "controller": "vera"})
        race = race_of(cells)
        state = ("green" if st["ok"] else "red" if st["ok"] is False else
                 ("racing" if st["executed"] else "none"))
        race["state"] = state
        lanes.append({"id": st["id"], "name": st["title"], "cells": cells, "hidden": 0,
                      "race": race, "state": state, "controllers": ["vera"],
                      "last_ts": "", "total": len(cells),
                      "passed": sum(1 for c in cells if c["o"] == "pass"),
                      "failed": sum(1 for c in cells if c["o"] == "fail")})
    run = t.get("run") or {}
    return {"kind": KIND, "view": "matrix", "title": title or _s(run.get("goal"))[:120] or "Loop",
            "group": "step", "source": "workshop.agent_loop.trace",
            "session_id": _s(t.get("session_id")), "run": run,
            "lanes": lanes, "summary": summary_of(lanes),
            "warnings": [_s(w) for w in (t.get("warnings") or [])]}


def loop_race(trace: Dict[str, Any], *, title: str = "") -> Dict[str, Any]:
    """A loop's race to green: completion-gate rounds and step outcomes in the
    order they happened, as one lane per step plus the gate lane."""
    m = loop_matrix(trace, title=title)
    gates = _rows((trace or {}).get("gates"))
    if gates:
        cells = [{"id": f"gate.{_i(g.get('round'))}", "ts": "",
                  "o": "pass" if g.get("complete") else "fail",
                  "label": f"round {_i(g.get('round'))}",
                  "summary": "; ".join(_s(x) for x in (g.get("missing") or [])) or "complete"}
                 for g in gates]
        race = race_of(cells)
        m["lanes"].insert(0, {"id": "gate", "name": "completion gate", "cells": cells, "hidden": 0,
                              "race": race, "state": race["state"], "controllers": ["vera"],
                              "last_ts": "", "total": len(cells),
                              "passed": sum(1 for c in cells if c["o"] == "pass"),
                              "failed": sum(1 for c in cells if c["o"] == "fail")})
    m["view"] = "race"
    m["summary"] = summary_of(m["lanes"])
    return m


def loop_board(trace: Dict[str, Any], *, title: str = "") -> Dict[str, Any]:
    """A loop's plan as a board: planned · running · done · failed."""
    t = trace or {}
    status = _s((t.get("run") or {}).get("status")).lower()
    cols: Dict[str, List[Dict[str, Any]]] = {"planned": [], "running": [], "done": [], "failed": []}
    steps = _loop_steps(t)
    for i, st in enumerate(steps):
        if st["ok"] is True:
            lane = "done"
        elif st["ok"] is False:
            lane = "failed"
        elif st["executed"] or (status == "running" and i == next(
                (j for j, s in enumerate(steps) if s["ok"] is None), -1)):
            lane = "running"
        else:
            lane = "planned"
        cols[lane].append({"id": st["id"], "title": st["title"], "lane": lane,
                           "calls": len(st["calls"]),
                           "ms": sum(_i(c.get("ms")) for c in st["calls"]),
                           "detail": st["success"]})
    columns = [{"name": k, "items": v} for k, v in cols.items()]
    run = t.get("run") or {}
    return {"kind": KIND, "view": "board", "title": title or _s(run.get("goal"))[:120] or "Loop plan",
            "session_id": _s(t.get("session_id")), "run": run, "columns": columns,
            "summary": {"items": len(steps), "by_lane": {k: len(v) for k, v in cols.items()},
                        "counters": t.get("counters") or {}}}


def loops_matrix(sessions: Optional[Iterable[Dict[str, Any]]], *, title: str = "") -> Dict[str, Any]:
    """Many loops at once: one lane per goal template (the goal's first words),
    one cell per session, so repeated goals race each other."""
    rows = []
    for s in _rows(sessions):
        goal = _s(s.get("goal"))
        rows.append({"id": _s(s.get("session_id")), "ts": _s(s.get("started_at")),
                     "status": _s(s.get("status")), "branch": " ".join(goal.split()[:6]) or "loop",
                     "summary": goal, "session_id": _s(s.get("session_id")),
                     "controller": "vera",
                     "ok": (True if _s(s.get("status")) == "done" else
                            False if _s(s.get("status")) in ("failed", "error", "cancelled", "timeout")
                            else None)})
    m = matrix(rows, title=title or "Agentic loops", source="workshop.agent_loop.sessions")
    return m


# merged branches, read from git log (ci.branch)

#: "Loop Lab: merge feat/x (pipeline 1a2b3c4d)" — the pipeline's own merge commit
_LL_MERGE = re.compile(r"merge\s+(\S+)\s+\(pipeline\s+([0-9a-f]{6,})\)", re.I)
#: "Merge branch 'x' into y" / "Merge remote-tracking branch 'origin/x'" / "Merge x into y"
_GIT_MERGE = re.compile(r"^Merge (?:remote-tracking )?branch '([^']+)'|^Merge (\S+) into \S+", re.I)


def parse_merges(log_out: str) -> List[Dict[str, Any]]:
    """`git log --merges --format=%H%x1f%cI%x1f%an%x1f%s` → one row per merged
    branch (newest merge wins, `merges` counts them all), with the pipeline id
    when Loop Lab made the merge."""
    seen: Dict[str, Dict[str, Any]] = {}
    for line in (log_out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        sha, ts, author, subject = parts[0], parts[1], parts[2], "\x1f".join(parts[3:])
        m = _LL_MERGE.search(subject)
        br, pid = (m.group(1), m.group(2)) if m else ("", "")
        if not br:
            g = _GIT_MERGE.search(subject)
            br = (g.group(1) or g.group(2)) if g else ""
        br = br.strip().rstrip(",")
        if br.startswith("origin/"):
            br = br[len("origin/"):]
        if not br:
            continue
        if br in seen:
            seen[br]["merges"] += 1
            continue
        seen[br] = {"branch": br, "pipeline_id": pid, "sha": sha[:10], "ts": ts,
                    "author": author, "subject": subject, "merges": 1}
    return list(seen.values())

# census x commits: what the census measured, run by run, and what landed between the runs

#: the measured noise floor of a census (runs 49/50, identical code): one capped goal and 13% of total wall time.
#: A change inside it is reported as noise, never as an improvement - "best run yet" is not a result.
NOISE_DONE = 1
NOISE_WALL = 0.13


def _run_outcome(r: Dict[str, Any]) -> str:
    st = _s(r.get("status")).lower()
    if r.get("ok") is True or st in ("done", "pass", "ok"):
        return "pass"
    if "cap" in st or "timeout" in st:
        return "cap"
    if r.get("ok") is False or st:
        return "fail"
    return "unknown"


def census_view(runs: Iterable[Dict[str, Any]], landed: Optional[Dict[str, Any]] = None,
                goal_results: Optional[Dict[str, Iterable[Dict[str, Any]]]] = None,
                *, template: str = "default", title: str = "") -> Dict[str, Any]:
    """The census, run by run, beside the commits that landed before each run.

    runs          census.runs rows (run_id, ended_at, goals, done, wall_capped, wall_total_s, quality_mean, ...)
    landed        census.landed's by_run: {run_id: {commits[], count, merges, window_from, window_to}}
    goal_results  {goal_id: [task-history results with driver (the run id), ok, status, wall_s]}

    Each run carries its deltas against the previous run and a verdict - `signal` when the change clears the
    noise floor, `noise` when it does not - so the view can point at the commits that preceded a REAL change
    and say plainly when a change was not one."""
    rs = [r for r in _rows(runs) if not template or _s(r.get("template") or "default") == template]
    rs.sort(key=lambda r: _s(r.get("ended_at")) or _s(r.get("run_id")))
    by_run = (landed or {}).get("by_run", landed or {}) if isinstance(landed, dict) else {}
    out: List[Dict[str, Any]] = []
    prev = None
    for r in rs:
        rid = _s(r.get("run_id"))
        goals = _i(r.get("goals"))
        done = _i(r.get("done"))
        wall = float(r.get("wall_total_s") or 0)
        land = by_run.get(rid) or {}
        commits = [{"sha": _s(c.get("sha")), "ts": c.get("ts"), "subject": _s(c.get("subject")),
                    "branch": _s(c.get("branch")), "pipeline": _s(c.get("pipeline")),
                    "merge": bool(c.get("is_merge")), "during_run": bool(c.get("during_run"))}
                   for c in _rows(land.get("commits"))]
        row = {"id": rid, "ended_at": _s(r.get("ended_at")), "goals": goals, "done": done,
               "capped": _i(r.get("wall_capped")), "wall_s": round(wall, 1),
               "quality": r.get("quality_mean"), "checks": [_i(r.get("checks_passed")), _i(r.get("checks_total"))],
               "codes": list(((r.get("code") or {}).get("codes") or [])),
               "commits": commits, "commit_count": len(commits),
               "merges": sum(1 for c in commits if c["merge"])}
        if prev is not None:
            dd = done - prev["done"]
            dw = (wall - prev["wall_s"]) / prev["wall_s"] if prev["wall_s"] else 0.0
            row["delta_done"] = dd
            row["delta_wall"] = round(dw, 3)
            sig_done = abs(dd) > NOISE_DONE
            sig_wall = abs(dw) > NOISE_WALL and dd == 0
            row["verdict"] = "signal" if (sig_done or sig_wall) else "noise"
            better = dd > 0 or (dd == 0 and dw < 0)
            row["direction"] = "up" if better and (dd or dw) else ("down" if (dd < 0 or (dd == 0 and dw > 0)) else "flat")
        out.append(row)
        prev = row
    goals_out: List[Dict[str, Any]] = []
    ids = [x["id"] for x in out]
    for gid, results in sorted((goal_results or {}).items()):
        cells = {}
        for res in _rows(results):
            drv = res.get("driver")
            drv = _s(drv.get("id") if isinstance(drv, dict) else drv)
            if drv in ids and drv not in cells:
                cells[drv] = _run_outcome(res)
        name = gid.split("census-%s-" % template, 1)[-1] if template else gid
        goals_out.append({"id": gid, "name": name, "cells": [cells.get(i, "") for i in ids],
                          "pass": sum(1 for v in cells.values() if v == "pass"), "runs": len(cells)})
    sig = [x for x in out if x.get("verdict") == "signal"]
    latest = out[-1] if out else {}
    return {"kind": KIND, "view": "census", "title": title or "Census \u00d7 commits", "template": template,
            "runs": out, "goals": goals_out,
            "summary": {"runs": len(out), "latest": latest.get("id", ""), "latest_done": latest.get("done"),
                        "goals": latest.get("goals"), "best_done": max((x["done"] for x in out), default=None),
                        "signals": len(sig), "signals_up": sum(1 for x in sig if x.get("direction") == "up"),
                        "commits": sum(x["commit_count"] for x in out),
                        "noise": {"done": NOISE_DONE, "wall": NOISE_WALL}}}
