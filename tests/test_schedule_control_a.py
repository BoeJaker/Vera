"""looplab-control Part A (17 Oct 2026): every test family can be scheduled and controlled.

A1 background work is followed to its real end; A2 quiet hours and declared activities hold starts;
A3 one exclusive run at a time and window-end stops for every kind; A4 the missing families are
allowed; A6 overnight windows.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import schedule_core as core  # noqa: E402

pytestmark = pytest.mark.critical
UTC = timezone.utc


def _rec(**kw):
    base = {"kind": "cap", "target": {"name": "bench.run"}, "timezone": "UTC", "days": list(range(7)),
            "start": "00:00", "end": "23:59", "repeat": "once_per_window"}
    base.update(kw)
    return core.normalize(base)


# ── A1 ────────────────────────────────────────────────────────────────────────
def test_background_starters_are_followed():
    assert core.follow_args("evolve.suite.start", {"ok": True, "started": True}) == ("evolve.suite.status", {})
    assert core.follow_args("bench.run.start", {"ok": True, "run_id": "r1"}) == ("bench.status", {"run_id": "r1"})
    assert core.follow_args("bench.run.start", {"ok": True}) is None          # no id - nothing to follow
    assert core.follow_args("chat.corpus.run", {"ok": False, "error": "busy"}) is None
    assert core.follow_args("bench.run", {"ok": True}) is None                # synchronous


def test_follow_state_reads_each_status_shape():
    assert core.follow_state("evolve.suite.start", {"running": True, "status": {}})[0] is False
    ended, word, _ = core.follow_state("evolve.suite.start", {"running": False, "status": {"avg_combined": 7.1}})
    assert ended and word == "done"
    assert core.follow_state("bench.matrix.start", {"state": "running"})[0] is False
    assert core.follow_state("bench.matrix.start", {"state": "error", "error": "x"})[:2] == (True, "error")
    assert core.follow_state("bench.run.start", {"runs": [{"state": "done"}]})[:2] == (True, "done")
    assert core.follow_state("evolve.script.run", {"state": "exited"})[:2] == (True, "done")
    # an unreadable status is not the end
    assert core.follow_state("bench.matrix.start", {"error": "redis unavailable"})[0] is False


# ── A2 ────────────────────────────────────────────────────────────────────────
def test_quiet_hour_holds_starts_unless_quiet_ok():
    now = datetime(2026, 10, 18, 4, 20, tzinfo=UTC)
    assert core.in_quiet(now, core.DEFAULT_QUIET, "UTC") == "04:00-05:00"
    assert core.in_quiet(datetime(2026, 10, 18, 5, 0, tzinfo=UTC), core.DEFAULT_QUIET, "UTC") == ""
    assert core.in_quiet(now, [{"start": "23:00", "end": "01:00"}], "UTC") == ""
    assert core.in_quiet(datetime(2026, 10, 18, 0, 30, tzinfo=UTC), [{"start": "23:00", "end": "01:00"}], "UTC")
    st = {"quiet": "04:00-05:00"}
    assert core.is_due(_rec(), now, st) == (False, "quiet hour 04:00-05:00 (background queue)")
    assert core.is_due(_rec(quiet_ok=True), now, st)[0] is True


def test_declared_activity_holds_exclusive_starts_and_censuses():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    st = {"declared_busy": "probe dg-boe-rate"}
    assert core.is_due(_rec(), now, st)[1].startswith("declared activity")
    assert core.is_due(_rec(exclusive=False), now, st)[0] is True
    census = _rec(kind="census", target={"template": "default"}, repeat="continuous")
    assert core.is_due(census, now, st)[1].startswith("declared activity")


# ── A3 ────────────────────────────────────────────────────────────────────────
def test_one_exclusive_run_at_a_time():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    assert core.is_due(_rec(), now, {"exclusive_running": 1}) == (False, "another exclusive scheduled run is going")
    assert core.is_due(_rec(exclusive=False), now, {"exclusive_running": 1})[0] is True
    tests = _rec(kind="tests", target={"branch": "bleeding-edge"})
    assert core.is_due(tests, now, {"exclusive_running": 1})[0] is False


def test_plan_tick_starts_at_most_one_exclusive_run():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    a = _rec(id="a", title="a")
    b = _rec(id="b", title="b", target={"name": "memory.perf.probe"})
    c = _rec(id="c", title="c", exclusive=False, target={"name": "obs.health"})
    plan = core.plan_tick([a, b, c], now, {})
    assert [p["schedule_id"] for p in plan] == ["a", "c"]


def test_window_end_stops_non_census_runs_that_asked():
    now = datetime(2026, 10, 18, 3, 0, tzinfo=UTC)
    rec = _rec(start="01:00", end="02:40", at_window_end="drop")
    assert core.window_end_stop(rec, now) is True
    assert core.window_end_stop(dict(rec, at_window_end="finish"), now) is False
    assert core.window_end_stop(rec, datetime(2026, 10, 18, 2, 0, tzinfo=UTC)) is False
    census = _rec(kind="census", target={"template": "default"}, at_window_end="drop")
    assert core.window_end_stop(census, now) is False      # census keeps its own yield/drop path


# ── A4 ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["ml.train", "ml.train.evaluate", "ml.sweep", "retrieval.eval",
                                  "retrieval.known_items.run", "dream.cycle.run", "evolve.script.run",
                                  "decide.student.nightly"])
def test_new_families_are_allowed(name):
    assert core.cap_allowed(name)


@pytest.mark.parametrize("name", ["ml.export.onnx", "ml.delete", "retrieval.known_items.build",
                                  "evolve.script.status_hack", "dream.trigger.delete"])
def test_neighbours_stay_refused(name):
    assert not core.cap_allowed(name)


# ── A6 ────────────────────────────────────────────────────────────────────────
def test_overnight_window():
    rec = _rec(days=[4], start="22:00", end="02:00")                  # Friday 22:00 -> Saturday 02:00
    fri_23 = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
    sat_01 = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
    sat_03 = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
    assert core.current_window(rec, fri_23) is not None
    assert core.current_window(rec, sat_01) is not None           # still Friday's window
    assert core.current_window(rec, sat_03) is None
    s, e = core.window_on(rec, fri_23.date())
    assert (e - s).total_seconds() == 4 * 3600
    with pytest.raises(ValueError):
        _rec(start="02:00", end="02:00")
