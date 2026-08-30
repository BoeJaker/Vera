"""The loop census as a readable history: runs over time, and what changed.

The census is the measuring instrument for the agentic loop — 12 goals, run
serially, one trace record per goal appended to `census.jsonl`. It has produced
a dozen runs, and until now reading it meant `tail`-ing a log by eye and holding
the previous run in your head. That is why run 10's counter anomalies sat
unexplained for a pass: nothing put two runs side by side.

This module owns the SHAPE of that history. It is deliberately pure — the files
are read by the capability layer, and everything here is a fold over records
that are already parsed — so the interesting logic (what counts as an
improvement, when a run's numbers may not be trusted) is testable without the
app, Redis, or a live census.

## Two things it is careful about

**A run is only comparable to another run per GOAL.** The goals differ wildly in
cost — `trivial-chat` is 65 seconds, `research-report` is capped at 25 minutes —
so a run-level average of wall time says nothing. Every comparison here is
per-goal, and a goal missing from either side is reported as such rather than
scored.

**Counters can be untrustworthy, and that must be visible.** Before L9
(2026-08-29), `executed_steps` could exceed `planned_steps` with
`inserted_steps: 0`, because completion-gate follow-ups were a second insertion
channel nothing counted. A run whose accounting does not reconcile —
`unaccounted > 0`, or the older runs that predate the counter entirely — is
marked `counters_reconcile: False`, so a number drawn from it is read with the
right amount of suspicion instead of quietly believed.

## ⚠ Step counts are a DIAGNOSTIC, never a verdict (user, 2026-08-30)

Nothing in this module decides whether a run achieved its goal, and the UI built
on it must not imply otherwise. A step count says how much happened, not whether
any of it was aimed at the goal:

  * fewer steps can mean a tighter plan, or a plan that quietly dropped half the
    request — run 12's `build-multifile` planned ONE step for a multi-file
    package;
  * more steps can mean thrashing, or a completion gate legitimately catching a
    missing deliverable and adding the step that finishes the job;
  * `status: done` is the harness's coarse outcome, and L8 exists precisely
    because a run could report success without reaching its goal.

So judging a run means reading its PROGRESSION and its OUTPUT: what the steps
were, whether they were reasonable and aimed at the goal, and what the run
actually produced. `goal_evidence()` assembles exactly that material from a
run's trace, and `compare_runs` labels its own `change` as
`outcome_change` — a status transition to investigate, not a score.

## Board linkage

Board items name the run that found them and the run that verified the fix,
through labels: `census:found:<run_id>` and `census:fixed:<run_id>`. Labels
rather than new BoardItem fields, because the board's schema is shared with
every other agent and a convention costs nothing to adopt or abandon.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# A run that hit the harness's wall cap did not fail — it ran out of the
# instrument's budget, which is a different fact and is reported separately.
STATUS_OK = "done"
WALL_CAP = "wall-cap"

FOUND_PREFIX = "census:found:"
FIXED_PREFIX = "census:fixed:"

# census.jsonl                → the run in progress (or the most recent one)
# census.run11.jsonl          → an archived run
# census.run10-prefixes.jsonl → an archived run with a note in its name
_RUN_FILE_RE = re.compile(r"^census(?:\.(?P<name>[^.]+))?\.jsonl$", re.I)
_RUN_NUM_RE = re.compile(r"run(\d+)", re.I)

CURRENT = "current"


def run_id_from_filename(filename: str) -> Optional[str]:
    """"census.run11.jsonl" -> "run11";  "census.jsonl" -> "current"."""
    m = _RUN_FILE_RE.match((filename or "").strip())
    if not m:
        return None
    return m.group("name") or CURRENT


def run_sort_key(run_id: str) -> Tuple[int, int, str]:
    """Chronological-ish order: numbered runs ascending, `current` always last.

    The files carry no timestamp, and mtime lies the moment one is copied, so
    the run NUMBER in the name is the only honest ordering signal available.
    """
    if run_id == CURRENT:
        return (2, 0, "")
    m = _RUN_NUM_RE.search(run_id or "")
    return (0, int(m.group(1)), run_id) if m else (1, 0, run_id or "")


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarise_run(run_id: str, records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """One run's headline numbers.

    `trusted` is the one to read first: it is False when any record's step
    accounting does not reconcile, which is exactly when a delta computed
    against this run may be measuring the instrument rather than the loop.
    """
    recs = [r for r in (records or []) if isinstance(r, dict)]
    done = [r for r in recs if r.get("status") == STATUS_OK]
    capped = [r for r in recs if r.get("status") == WALL_CAP]
    unacc = sum(int(r.get("unaccounted") or 0) for r in recs)
    # Runs predating L9 have no `unaccounted` key at all. Absent is not zero:
    # it means nobody was counting, which is its own reason for suspicion.
    measured = [r for r in recs if r.get("unaccounted") is not None]
    walls = [w for w in (_num(r.get("wall_s")) for r in recs) if w is not None]
    return {
        "run_id": run_id,
        "goals": len(recs),
        "done": len(done),
        "wall_capped": len(capped),
        "other": len(recs) - len(done) - len(capped),
        "unaccounted_total": unacc,
        "gate_inserted_total": sum(int(r.get("gate_inserted") or 0) for r in recs),
        "fast_path_total": sum(int(r.get("fast_path") or 0) for r in recs),
        "warnings_total": sum(len(r.get("warnings") or []) for r in recs),
        "wall_total_s": round(sum(walls), 1),
        "accounting_measured": len(measured),
        # Names exactly what it checks: the step counters add up. It is NOT a
        # claim that the run's goals were achieved — see the module docstring.
        "counters_reconcile": bool(recs) and len(measured) == len(recs) and unacc == 0,
    }


def goal_evidence(record: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    """The material needed to JUDGE one goal — not to score it.

    Step counts say how much happened, never whether it was aimed at the goal,
    so this returns what a person actually has to read: what the run was asked
    to satisfy (`done_when`), the steps it chose and the caps they used, whether
    the completion gate thought anything was still missing, and what it
    produced. Deliberately returns no verdict of its own.
    """
    plan = (trace or {}).get("plan") or {}
    steps_plan = {str(s.get("id")): s for s in (plan.get("steps") or [])}
    steps: List[Dict[str, Any]] = []
    for s in (trace or {}).get("steps") or []:
        sid = str(s.get("step_id"))
        p = steps_plan.get(sid) or {}
        calls = s.get("calls") or []
        steps.append({
            "id": s.get("step_id"),
            "title": s.get("title") or p.get("title") or "",
            "caps": p.get("caps") or [],
            "success": p.get("success") or "",
            "ok": s.get("ok"),
            "cycles": len(calls),
            "tools": sorted({str(c.get("tool")) for c in calls if c.get("tool")}),
            "failed_tools": sorted({str(c.get("tool")) for c in calls
                                    if c.get("ok") is False}),
            # A step the planner never listed is one to look at hardest: it is
            # either a gate follow-up doing real work, or an undeclared producer.
            "planned": sid in steps_plan,
        })
    gates = (trace or {}).get("gates") or []
    last_gate = gates[-1] if gates else {}
    return {
        "id": record.get("id"),
        "goal_status": record.get("status"),
        "wall_s": record.get("wall_s"),
        "done_when": plan.get("done_when") or "",
        "tier": plan.get("tier"), "intent": plan.get("intent"),
        "steps": steps,
        "unplanned_steps": [s["id"] for s in steps if not s["planned"]],
        "gate_complete": last_gate.get("complete"),
        "gate_missing": last_gate.get("missing") or [],
        "gate_rounds": len(gates),
        "warnings": (trace or {}).get("warnings") or [],
        "how_to_read": ("Judge this by the progression and the output: were these "
                        "steps reasonable and aimed at the goal, and did the run "
                        "produce what done_when asks for? Step counts alone settle "
                        "nothing."),
    }


def _outcome_rank(status: str) -> int:
    """Order outcomes worst→best so a change has a direction.

    `wall-cap` outranks the failure statuses because the run was still working
    when the instrument stopped it — that is closer to success than a run that
    fell over, and treating them alike hides real progress.
    """
    return {STATUS_OK: 3, WALL_CAP: 2}.get(str(status or ""), 1)


def compare_runs(base: Sequence[Dict[str, Any]],
                 head: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-goal OUTCOME transition from `base` to `head`, worst news first.

    Per goal and never aggregated: the goals differ by two orders of magnitude
    in cost, so a run-level average would be meaningless.

    `outcome_change` is a status transition worth investigating, NOT a score.
    A goal can move to `done` having quietly dropped half the request, and one
    that stays `wall-cap` can be doing markedly better work than last time.
    Read `goal_evidence()` before believing any of these labels.
    """
    b = {r.get("id"): r for r in (base or []) if isinstance(r, dict)}
    h = {r.get("id"): r for r in (head or []) if isinstance(r, dict)}
    out: List[Dict[str, Any]] = []
    for gid in sorted(set(b) | set(h), key=lambda x: str(x)):
        br, hr = b.get(gid), h.get(gid)
        if br is None or hr is None:
            out.append({"id": gid, "outcome_change": "missing",
                        "base_status": (br or {}).get("status"),
                        "head_status": (hr or {}).get("status"),
                        "note": "not present in both runs — not comparable"})
            continue
        d = _outcome_rank(hr.get("status")) - _outcome_rank(br.get("status"))
        change = "improved" if d > 0 else ("regressed" if d < 0 else "held")
        bw, hw = _num(br.get("wall_s")), _num(hr.get("wall_s"))
        rec: Dict[str, Any] = {
            "id": gid, "outcome_change": change,
            "base_status": br.get("status"), "head_status": hr.get("status"),
            "base_wall_s": bw, "head_wall_s": hw,
            "wall_delta_s": (round(hw - bw, 1) if bw is not None and hw is not None else None),
            "base_planned": br.get("planned"), "head_planned": hr.get("planned"),
            "base_executed": br.get("executed"), "head_executed": hr.get("executed"),
            "head_unaccounted": hr.get("unaccounted"),
            "head_gate_inserted": hr.get("gate_inserted"),
        }
        if hr.get("unaccounted"):
            rec["note"] = ("this run has steps no producer claims — its counters "
                           "cannot settle whether the goal really improved")
        out.append(rec)
    order = {"regressed": 0, "missing": 1, "held": 2, "improved": 3}
    out.sort(key=lambda r: (order.get(r["outcome_change"], 4), str(r["id"])))
    return out


def run_ids_from_labels(labels: Iterable[str], prefix: str) -> List[str]:
    """Run ids a board item names under one label prefix."""
    out: List[str] = []
    for lab in labels or []:
        s = str(lab).strip()
        if s.lower().startswith(prefix):
            rid = s[len(prefix):].strip()
            if rid:
                out.append(rid)
    return out


def board_links_by_run(items: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Map run_id -> {found: [...], fixed: [...]} from board item labels.

    This is what makes a census run answerable as "which fixes does this run
    contain, and what did it newly expose" instead of a wall of numbers.
    """
    by_run: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    def _slot(rid: str, key: str) -> List[Dict[str, Any]]:
        return by_run.setdefault(rid, {"found": [], "fixed": []})[key]

    for it in items or []:
        if not isinstance(it, dict):
            continue
        brief = {"id": it.get("id"), "title": it.get("title"),
                 "lane": it.get("lane"), "branch": it.get("branch"),
                 "pipeline": it.get("pipeline")}
        labels = it.get("labels") or []
        for rid in run_ids_from_labels(labels, FOUND_PREFIX):
            _slot(rid, "found").append(brief)
        for rid in run_ids_from_labels(labels, FIXED_PREFIX):
            _slot(rid, "fixed").append(brief)
    return by_run


def history(summaries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The coarse 'are we getting better' view: done-count across runs.

    A trend line, not a verdict. `done` is the harness's outcome status, and a
    goal can reach it having dropped half the request — so this says where to
    look, and `goal_evidence()` says what actually happened.

    **A PARTIAL RUN IS NOT COMPARABLE AND IS EXCLUDED.** Half the archived files
    are partial (`census.run4-partial.jsonl` has one goal), and the live
    `census.jsonl` is partial for the hours a run takes. Counting those in a
    done-count trend produces a confident, wrong answer: measured against the
    real files this reported `run1 → current, done 2 → 1, delta −1` — "we got
    worse" — while run 12 was simply three goals into twelve. A run is treated
    as complete only if it covers as many goals as the fullest run present.

    The trend over runs whose counters also reconcile is reported SEPARATELY: an
    improvement measured across a run whose own accounting did not add up is not
    evidence, and averaging the two would launder it into one number.
    """
    ordered = sorted(summaries or [], key=lambda s: run_sort_key(str(s.get("run_id") or "")))
    # The goal set is defined by the harness, not by us, so the fullest run
    # present is the only available definition of "a complete pass".
    full = max((int(s.get("goals") or 0) for s in ordered), default=0)
    ordered = [dict(s, partial=(int(s.get("goals") or 0) < full)) for s in ordered]
    complete = [s for s in ordered if not s["partial"]]
    trusted = [s for s in complete if s.get("counters_reconcile")]

    def _span(rows):
        if len(rows) < 2:
            return None
        return {"from": rows[0]["run_id"], "to": rows[-1]["run_id"],
                "done_from": rows[0]["done"], "done_to": rows[-1]["done"],
                "delta": rows[-1]["done"] - rows[0]["done"],
                "of_goals": full}
    return {"runs": ordered, "count": len(ordered),
            "full_goal_count": full,
            "complete_count": len(complete), "partial_count": len(ordered) - len(complete),
            "trusted_count": len(trusted),
            # Named for what it actually spans. There is deliberately NO trend
            # over every run: that number could only ever mislead.
            "trend_complete": _span(complete), "trend_trusted": _span(trusted)}
