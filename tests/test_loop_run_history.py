"""One durable record per loop run, retained by policy - not by a resume window.

Chat's Loops pane aged out at 7 days because its run hash carried _RESUME_TTL,
a window sized for RESUMING a run; and its zset index had no trim, so it grew
forever as tombstones pointing at expired hashes. That is why the pane showed 9
records one hour and 22 the next, and why a census could not be built from real
traffic.

Policy (user, 2026-08-27): keep the last 2,000 runs AND 90 days, both settable
from the UI. Unprojected runs are explicitly NOT filed into the project store.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag.loop_run_history import (          # noqa: E402
    DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_RUNS, ids_to_drop, normalise_config,
    summarise_run,
)

DAY = 86400.0
NOW = 1_800_000_000.0


# â”€â”€ retention policy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_defaults_are_the_agreed_policy():
    cfg = normalise_config(None)
    assert cfg["max_runs"] == DEFAULT_MAX_RUNS == 2000
    assert cfg["max_age_days"] == DEFAULT_MAX_AGE_DAYS == 90


def test_ui_can_change_both_bounds():
    cfg = normalise_config({"max_runs": 500, "max_age_days": 30})
    assert cfg == {"max_runs": 500, "max_age_days": 30}


@pytest.mark.parametrize("bad", [
    {"max_runs": "banana"}, {"max_runs": None}, {"max_age_days": ""},
    {"max_runs": 0}, {"max_age_days": -1}, {"max_runs": 10 ** 9},
])
def test_a_bad_setting_never_means_keep_nothing_or_keep_everything(bad):
    """The two failure modes that matter: silent data loss, or an unbounded store."""
    cfg = normalise_config(bad)
    assert 10 <= cfg["max_runs"] <= 100_000
    assert 1 <= cfg["max_age_days"] <= 3650


def test_runs_beyond_the_count_are_dropped_oldest_first():
    # max_runs must be >= MIN_MAX_RUNS (10) or the clamp raises it and nothing
    # is dropped - which is exactly how this test failed first time round.
    scored = [("s%d" % i, NOW - i) for i in range(14)]
    drop = ids_to_drop(scored, {"max_runs": 10, "max_age_days": 3650}, now=NOW)
    assert drop == ["s10", "s11", "s12", "s13"]


def test_runs_older_than_the_age_bound_are_dropped_even_if_few():
    """A count bound alone lets a quiet month age out invisibly - and vice versa."""
    scored = [("fresh", NOW - DAY), ("stale", NOW - 100 * DAY)]
    drop = ids_to_drop(scored, {"max_runs": 2000, "max_age_days": 90}, now=NOW)
    assert drop == ["stale"]


def test_both_bounds_apply_together():
    scored = ([("recent%d" % i, NOW - i) for i in range(3)]
              + [("old%d" % i, NOW - 200 * DAY - i) for i in range(3)])
    drop = ids_to_drop(scored, {"max_runs": 5, "max_age_days": 90}, now=NOW)
    assert set(drop) == {"old0", "old1", "old2"}


def test_order_is_derived_not_trusted():
    """The caller reads a zset a concurrent writer may have reordered."""
    scored = ([("old%d" % i, NOW - (50 + i) * DAY) for i in range(3)]
              + [("new", NOW)]
              + [("mid%d" % i, NOW - (10 + i) * DAY) for i in range(8)])
    drop = ids_to_drop(scored, {"max_runs": 10, "max_age_days": 3650}, now=NOW)
    assert "new" not in drop, "the newest must never be dropped"
    assert set(drop) <= {"old0", "old1", "old2"}, "only the OLDEST fall off: %s" % drop
    assert len(drop) == 2


def test_a_below_floor_count_is_raised_not_honoured():
    """Pinning the clamp: max_runs=1 must not mean 'keep one run'."""
    cfg = normalise_config({"max_runs": 1})
    assert cfg["max_runs"] == 10, "below-floor values are raised to MIN_MAX_RUNS"
    scored = [("s%d" % i, NOW - i) for i in range(12)]
    assert ids_to_drop(scored, {"max_runs": 1, "max_age_days": 3650}, now=NOW) == ["s10", "s11"]


def test_nothing_is_dropped_when_inside_both_bounds():
    scored = [("a", NOW), ("b", NOW - DAY)]
    assert ids_to_drop(scored, None, now=NOW) == []


def test_empty_input_is_safe():
    assert ids_to_drop([], None, now=NOW) == []


# â”€â”€ record shape â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_summary_carries_what_a_history_list_and_a_census_need():
    rec = summarise_run(
        {"session_id": "s1", "goal": "build a thing", "engine": "v7",
         "status": "done", "started_at": "T0", "updated_at": "T1"},
        {"planned_steps": 3, "executed_steps": 3, "inserted_steps": 0,
         "tool_calls": 9, "cycles_per_step": {"1": 2}},
        warnings=["w1", "w2"])
    assert rec["session_id"] == "s1"
    assert rec["planned_steps"] == "3" and rec["inserted_steps"] == "0"
    assert rec["warnings_count"] == "2"
    assert rec["status"] == "done"


def test_absent_fields_are_omitted_not_zeroed():
    """`planned_steps=0` would be a LIE a census cannot distinguish from 'no steps'."""
    rec = summarise_run({"session_id": "s1", "status": "running"}, {})
    assert "planned_steps" not in rec
    assert "inserted_steps" not in rec
    assert "warnings_count" not in rec, "no warnings list given != zero warnings"


def test_warnings_count_of_zero_is_recorded_because_it_was_measured():
    rec = summarise_run({"session_id": "s1"}, {}, warnings=[])
    assert rec["warnings_count"] == "0"


def test_goal_is_bounded():
    rec = summarise_run({"session_id": "s1", "goal": "x" * 5000}, {})
    assert len(rec["goal"]) <= 800


def test_project_slug_is_carried_but_not_required():
    """Projects keep their own store; this record just NOTES the association."""
    assert summarise_run({"session_id": "s", "project_slug": "vera"}, {})["project_slug"] == "vera"
    assert "project_slug" not in summarise_run({"session_id": "s"}, {})


def test_every_value_is_a_string_for_the_redis_hash():
    rec = summarise_run({"session_id": "s1", "status": "done"},
                        {"planned_steps": 3, "cycles_per_step": {"1": 2}},
                        warnings=[])
    assert all(isinstance(v, str) for v in rec.values()), rec
