"""Measuring browser runs over time, without laundering their self-reports.

The loop census exists because the agentic loop needed a measuring instrument.
operator.run needs one for the same reason: across loop-census runs 12 and 13,
EVERY goal with an operator.run step consumed its entire 25-minute wall cap,
while every goal without one finished in under seven minutes.

The one thing this must not do is reuse the loop's scoring, because the two fail
differently. A browser run's most dangerous state is not "slow" - it is
"ran out of steps and still says it is done", which is the failure L8 was landed
for. So the ladder deliberately ranks a CEILING run BELOW one that errored
honestly, and there is no `success` rank at all: `finished` means ran cleanly to
a stop, never that the goal was achieved.

Pure: no Redis, no browser, no app import.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.census.operator_census_core import (          # noqa: E402
    CEILING, ERRORED, FINISHED, INCOMPLETE, compare_runs, outcome_of,
    record_from_trace, summarise_run,
)


def _rec(gid, *, completed=True, ceiling=False, errors=0, steps=5,
         repeated=0, duration=60.0):
    return {"id": gid, "run_id": "r-" + gid, "completed": completed,
            "ceiling_hit": ceiling, "errors": errors, "steps": steps,
            "repeated_actions": repeated, "duration_s": duration}


# ── the outcome ladder ──────────────────────────────────────────────────────
def test_a_clean_run_is_finished():
    assert outcome_of(_rec("g")) == FINISHED


def test_a_run_with_step_errors_is_errored():
    assert outcome_of(_rec("g", errors=2)) == ERRORED


def test_a_run_that_ran_out_of_steps_is_ceiling():
    assert outcome_of(_rec("g", ceiling=True)) == CEILING


def test_a_run_with_no_completion_event_is_incomplete():
    assert outcome_of(_rec("g", completed=False)) == INCOMPLETE


def test_ceiling_outranks_errors_when_both_are_true():
    """An unreliable self-report is worse than a visible failure: a run at its
    ceiling may still say 'done' while having merely run out of steps."""
    assert outcome_of(_rec("g", ceiling=True, errors=3)) == CEILING


def test_incomplete_beats_everything():
    assert outcome_of(_rec("g", completed=False, ceiling=True, errors=9)) == INCOMPLETE


def test_junk_is_incomplete_not_finished():
    """Fail toward 'we cannot vouch for this', never toward success."""
    assert outcome_of(None) == INCOMPLETE
    assert outcome_of({}) == INCOMPLETE


def test_there_is_no_success_rank():
    """`finished` is 'ran cleanly to a stop'. Whether the browser did the thing
    is not decidable here, and a rank called success would imply otherwise."""
    from vera.census import operator_census_core as m
    assert not hasattr(m, "SUCCESS")
    assert "success" not in m._RANK and "achieved" not in m._RANK


# ── run summaries ───────────────────────────────────────────────────────────
def test_a_run_summarises_by_outcome():
    s = summarise_run("run1", [_rec("a"), _rec("b", errors=1),
                               _rec("c", ceiling=True), _rec("d", completed=False)])
    assert (s["finished"], s["errored"], s["ceiling"], s["incomplete"]) == (1, 1, 1, 1)
    assert s["goals"] == 4


def test_a_run_containing_an_incomplete_goal_is_not_comparable():
    """An incomplete goal has no trustworthy duration or step count, so a
    comparison drawn against it would be measuring nothing."""
    assert summarise_run("r", [_rec("a"), _rec("b", completed=False)])["comparable"] is False
    assert summarise_run("r", [_rec("a"), _rec("b", errors=2)])["comparable"] is True


def test_thrashing_goals_are_counted_not_summed():
    """How many GOALS thrashed, not how many repeated actions there were - one
    pathological goal must not look like several."""
    s = summarise_run("r", [_rec("a", repeated=7), _rec("b", repeated=1), _rec("c")])
    assert s["thrashing_goals"] == 2


def test_totals_add_up():
    s = summarise_run("r", [_rec("a", steps=3, errors=1, duration=10.0),
                            _rec("b", steps=4, errors=2, duration=5.5)])
    assert s["steps_total"] == 7 and s["errors_total"] == 3
    assert s["duration_total_s"] == 15.5


def test_an_empty_run_is_not_comparable():
    assert summarise_run("r", [])["comparable"] is False


# ── comparison ──────────────────────────────────────────────────────────────
def test_comparison_is_per_goal_worst_news_first():
    base = [_rec("g1"), _rec("g2", ceiling=True), _rec("g3", errors=1)]
    head = [_rec("g1", ceiling=True), _rec("g2"), _rec("g3", errors=1)]
    out = compare_runs(base, head)
    assert [g["outcome_change"] for g in out] == ["regressed", "held", "improved"]
    assert [g["id"] for g in out] == ["g1", "g3", "g2"]


def test_moving_off_the_ceiling_counts_as_improvement():
    out = compare_runs([_rec("g", ceiling=True)], [_rec("g", errors=1)])
    assert out[0]["outcome_change"] == "improved"      # ceiling ranks below errored


def test_a_ceiling_head_is_annotated_as_settling_nothing():
    out = compare_runs([_rec("g")], [_rec("g", ceiling=True)])
    assert "settles nothing" in out[0]["note"]


def test_an_incomplete_head_says_its_numbers_mean_nothing():
    out = compare_runs([_rec("g")], [_rec("g", completed=False)])
    assert "mean nothing" in out[0]["note"]


def test_thrash_is_surfaced_on_an_otherwise_unremarkable_goal():
    out = compare_runs([_rec("g")], [_rec("g", repeated=4)])
    assert out[0]["outcome_change"] == "held"
    assert "thrash signature" in out[0]["note"]


def test_a_goal_in_only_one_run_is_not_scored():
    out = compare_runs([_rec("g1")], [_rec("g1"), _rec("g2")])
    g2 = [g for g in out if g["id"] == "g2"][0]
    assert g2["outcome_change"] == "missing" and "not comparable" in g2["note"]


# ── building a record from a trace ─────────────────────────────────────────
TRACE = {
    "run_id": "abc123",
    "run": {"run_id": "abc123", "goal": "verify the error appears",
            "target": "url", "reason": "max_steps"},
    "counters": {"steps": 12, "errors": 2, "screenshots": 12,
                 "repeated_actions": 1, "ceiling_hit": True},
    "duration_s": 1500.0, "ended_at": "2026-08-30T10:25:00Z",
    "warnings": ["the same action was attempted 5x: click #submit"],
}


def test_a_census_record_keeps_the_run_id_so_the_row_opens_into_the_trace():
    """The whole point of doing O13 first: a census row must lead back to the
    step-by-step, which the loop census could never do for its browser goals."""
    r = record_from_trace("browser-verify", TRACE)
    assert r["id"] == "browser-verify" and r["run_id"] == "abc123"


def test_a_record_carries_the_ceiling_and_the_runs_own_reason_together():
    r = record_from_trace("g", TRACE)
    assert r["ceiling_hit"] is True and r["reason"] == "max_steps"
    assert outcome_of(r) == CEILING


def test_a_trace_with_no_end_becomes_an_incomplete_record():
    t = dict(TRACE); t.pop("ended_at")
    r = record_from_trace("g", t)
    assert r["completed"] is False and outcome_of(r) == INCOMPLETE


def test_record_from_an_empty_trace_does_not_explode():
    r = record_from_trace("g", {})
    assert r["id"] == "g" and r["steps"] == 0 and outcome_of(r) == INCOMPLETE
