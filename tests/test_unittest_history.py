""""Race to green" could not show a race, because nothing kept the runs.

The strip read `evolve.pipeline.list` and drew one cell per PIPELINE coloured by
`gate_passed` — a scalar overwritten on every re-gate. A branch that went red,
got fixed and went green rendered as one green cell. Meanwhile the gate parsed
{passed, failed, errors, skipped} on every run and threw it away.

The sharpest test in here is `regressions`. A green run with FEWER tests than
the run before it is not a pass; it is coverage that stopped being collected,
and the gate reports PASS the whole way through. This codebase has already been
bitten by exactly that — a guard "sat broken for days" because nothing ran it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve import unittest_history as UH             # noqa: E402


def row(ts, ok, passed, failed=0, total=None, markers="critical", branch="feat/x",
        errors=0, skipped=0):
    return {"ts": ts, "ok": ok, "passed": passed, "failed": failed,
            "errors": errors, "skipped": skipped,
            "total": passed + failed + errors + skipped if total is None else total,
            "markers": markers, "paths": "tests", "branch": branch}


# A real sequence: red, red, fixed, then tests added.
RUNS = [
    row("2026-09-07T10:00:00Z", False, 2700, failed=3),
    row("2026-09-07T11:00:00Z", False, 2701, failed=2),
    row("2026-09-07T12:00:00Z", True, 2738),
    row("2026-09-07T13:00:00Z", True, 2755),
    row("2026-09-07T14:00:00Z", True, 2799),
]


# ── the record ──────────────────────────────────────────────────────────────
def test_a_run_is_recorded_with_its_counts():
    r = UH.record({"ok": True, "passed": 2799, "failed": 0, "errors": 0,
                   "skipped": 3, "rc": 0, "summary": "2799 passed"},
                  branch="feat/x", markers="critical", ts="2026-09-07T14:00:00Z")
    assert r["passed"] == 2799 and r["skipped"] == 3
    assert r["total"] == 2802, "total counts every outcome, not just passes"
    assert r["ok"] is True and r["branch"] == "feat/x"


def test_a_malformed_summary_does_not_explode():
    r = UH.record({"ok": False, "passed": None, "failed": "two"}, branch="b")
    assert r["passed"] == 0 and r["failed"] == 0 and r["total"] == 0


def test_the_label_stands_in_for_a_missing_branch():
    assert UH.record({}, label="primary")["branch"] == "primary"


# ── the race ────────────────────────────────────────────────────────────────
def test_the_red_to_green_transition_is_recoverable():
    """The whole point: two red runs, then green. A per-pipeline scalar could
    only ever have shown the final green."""
    r = UH.race_to_green(RUNS)
    assert r["red_runs"] == 2
    assert r["went_red_at"] == "2026-09-07T10:00:00Z"
    assert r["went_green_at"] == "2026-09-07T12:00:00Z"


def test_a_run_still_red_says_so_rather_than_reporting_a_win():
    rows = RUNS[:2]
    r = UH.race_to_green(rows)
    assert r["still_red"] is True and r["red_runs"] == 2
    assert "went_green_at" not in r


def test_the_most_recent_race_wins_not_the_first():
    rows = RUNS + [row("2026-09-07T15:00:00Z", False, 2790, failed=9),
                   row("2026-09-07T16:00:00Z", True, 2799)]
    assert UH.race_to_green(rows)["went_green_at"] == "2026-09-07T16:00:00Z"


def test_an_all_green_history_has_no_race():
    assert UH.race_to_green(RUNS[2:]) == {}


def test_the_green_streak_counts_from_the_newest_end():
    assert UH.green_streak(RUNS) == 3
    assert UH.green_streak(RUNS[:2]) == 0


def test_the_streak_STOPS_at_the_first_red_it_meets():
    """A streak is consecutive, not a tally. With a red in the middle, an
    all-greens count would say 3 where the truth is 2 — and "3 green in a row"
    when the suite broke an hour ago is exactly the wrong reassurance."""
    rows = [row("2026-09-07T10:00:00Z", True, 2700),
            row("2026-09-07T11:00:00Z", False, 2690, failed=8),
            row("2026-09-07T12:00:00Z", True, 2755),
            row("2026-09-07T13:00:00Z", True, 2799)]
    assert UH.green_streak(rows) == 2


# ── tests over time ─────────────────────────────────────────────────────────
def test_the_trend_is_a_number():
    t = UH.trend(RUNS)
    assert t["runs"] == 5
    assert t["passed"] == 2799
    assert t["passed_delta"] == 99
    assert t["green"] is True and t["green_streak"] == 3


def test_an_empty_history_yields_an_empty_trend():
    assert UH.trend([]) == {} and UH.trend(None) == {}


# ── the sharp one ───────────────────────────────────────────────────────────
def test_tests_that_stop_running_are_reported_even_though_the_run_is_green():
    """55 tests vanish, nothing fails, the gate says PASS. That is a module
    that stopped being collected, and it must not read as success."""
    rows = RUNS + [row("2026-09-07T15:00:00Z", True, 2744)]
    regs = UH.regressions(rows)
    assert len(regs) == 1
    assert regs[0]["lost"] == 55 and regs[0]["now"] == 2744
    assert "stopped running" in regs[0]["note"]


def test_a_run_that_loses_tests_because_they_FAILED_is_not_a_regression():
    """Failures are already loud. This finding is for the silent case."""
    rows = RUNS + [row("2026-09-07T15:00:00Z", False, 2740, failed=4)]
    assert UH.regressions(rows) == []


def test_growing_the_suite_is_never_a_regression():
    assert UH.regressions(RUNS) == []


def test_the_critical_tier_is_not_compared_against_the_full_suite():
    """They legitimately have different totals; comparing across them would
    report a regression on every alternating run."""
    rows = [row("2026-09-07T10:00:00Z", True, 2799, markers="critical"),
            row("2026-09-07T11:00:00Z", True, 400, markers=""),
            row("2026-09-07T12:00:00Z", True, 2799, markers="critical")]
    assert UH.regressions(rows) == []


# ── the strip ───────────────────────────────────────────────────────────────
def test_the_strip_is_one_cell_per_run_oldest_first():
    cells = UH.lanes(RUNS)
    assert len(cells) == 5
    assert cells[0]["ts"] < cells[-1]["ts"]
    assert [c["ok"] for c in cells] == [False, False, True, True, True]


def test_each_cell_knows_what_changed():
    cells = UH.lanes(RUNS)
    assert cells[0]["delta"] == 0, "the first cell has nothing to compare against"
    assert cells[3]["delta"] == 17
    assert cells[4]["delta"] == 44


def test_the_strip_is_bounded_and_keeps_the_NEWEST():
    cells = UH.lanes(RUNS, limit=2)
    assert len(cells) == 2
    assert cells[-1]["ts"] == "2026-09-07T14:00:00Z"


def test_an_empty_history_renders_nothing_rather_than_failing():
    assert UH.lanes([]) == [] and UH.lanes(None) == []


# ── robustness ──────────────────────────────────────────────────────────────
def test_rows_with_no_timestamp_do_not_crash_the_sort():
    rows = [row("", True, 10), row("2026-09-07T10:00:00Z", True, 20)]
    assert len(UH.newest_first(rows)) == 2
    assert UH.newest_first(rows)[0]["passed"] == 20


def test_non_dict_junk_is_ignored():
    assert UH.newest_first([None, "x", 3, row("2026-09-07T10:00:00Z", True, 1)]) \
        and len(UH.newest_first([None, "x", row("2026-09-07T10:00:00Z", True, 1)])) == 1


def test_the_cap_is_sane():
    assert 50 <= UH.HISTORY_CAP <= 5000
