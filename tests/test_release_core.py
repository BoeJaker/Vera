"""A prod release must not restart prod under a census goal unless forced.

Pins the census gate on the release (2026-09-21): `finish` waits for the
whole census, `goal` yields and goes once the harness has parked, `force`
goes now; a pending release moves waiting -> release -> restarting -> finish
(resuming a census it yielded) and fails if the restart never comes back.
Pure: the census is the /health summary dict, the clock is an argument.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.evolve import release_core as rc  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

RUNNING = {"busy": True, "state": "running", "goal": "analyse-data", "control": "run"}
YIELDING = {"busy": True, "state": "yielding", "goal": "analyse-data", "control": "yield", "by": "evolve.release"}
YIELDED = {"busy": False, "state": "yielded", "control": "yield", "by": "evolve.release"}
PAUSED = {"busy": False, "state": "paused", "control": "pause", "by": "BoeJaker"}
DONE = {"busy": False, "state": "done", "control": "run"}
NONE = {"busy": False, "state": "none", "control": "run"}
STALE = {"busy": False, "state": "stale", "control": "run"}


def pend(mode="finish", **over):
    p = rc.new_pending(edge="bleeding-edge", mode=mode, by="claude", reason="r", restart=True,
                       now=T0, release_id="abc")
    p.update(over)
    return p


def test_may_release_by_mode():
    for c in (RUNNING, YIELDING):
        assert rc.may_release("finish", c)[0] is False
        assert rc.may_release("goal", c)[0] is False
        assert rc.may_release("force", c) == (True, "forced")
    assert rc.may_release("finish", YIELDED)[0] is False          # parked is not finished
    assert rc.may_release("finish", PAUSED)[0] is False
    assert rc.may_release("goal", YIELDED)[0] is True
    assert rc.may_release("goal", PAUSED)[0] is True
    for c in (DONE, NONE, STALE):
        assert rc.may_release("finish", c)[0] is True
        assert rc.may_release("goal", c)[0] is True


def test_finish_mode_waits_for_the_census_then_releases():
    p = pend("finish")
    assert rc.decide(p, RUNNING, now=T0, process_started_at=T0 - timedelta(days=1))["action"] == "wait"
    assert rc.decide(p, YIELDED, now=T0, process_started_at=T0 - timedelta(days=1))["action"] == "wait"
    assert rc.decide(p, DONE, now=T0, process_started_at=T0 - timedelta(days=1))["action"] == "release"


def test_goal_mode_yields_once_then_releases_when_parked():
    p = pend("goal")
    assert rc.decide(p, RUNNING, now=T0, process_started_at=None)["action"] == "yield"
    p["yield_written"] = True
    assert rc.decide(p, YIELDING, now=T0, process_started_at=None)["action"] == "wait"
    assert rc.decide(p, YIELDED, now=T0, process_started_at=None)["action"] == "release"
    # A census that ends on its own also clears the way.
    assert rc.decide(pend("goal"), DONE, now=T0, process_started_at=None)["action"] == "release"


def test_force_releases_at_once():
    assert rc.decide(pend("force"), RUNNING, now=T0, process_started_at=None)["action"] == "release"


def test_restart_completes_when_the_new_process_is_up_and_resumes_what_it_yielded():
    p = pend("goal", status="restarting", yield_written=True,
             restart_requested_at=(T0 + timedelta(minutes=5)).isoformat())
    old = T0 - timedelta(hours=1)
    assert rc.decide(p, YIELDED, now=T0 + timedelta(minutes=6), process_started_at=old)["action"] == "wait"
    new = T0 + timedelta(minutes=6)
    act = rc.decide(p, YIELDED, now=T0 + timedelta(minutes=7), process_started_at=new)
    assert act == {"action": "finish", "resume_census": True}
    p2 = pend("finish", status="restarting", restart_requested_at=(T0 + timedelta(minutes=5)).isoformat())
    assert rc.decide(p2, DONE, now=T0 + timedelta(minutes=7), process_started_at=new)["resume_census"] is False


def test_a_restart_that_never_comes_back_fails():
    p = pend("finish", status="restarting", restart_requested_at=T0.isoformat())
    assert rc.decide(p, DONE, now=T0 + timedelta(minutes=16), process_started_at=T0 - timedelta(days=1))["action"] == "fail"
    assert rc.decide(p, DONE, now=T0 + timedelta(minutes=5), process_started_at=T0 - timedelta(days=1))["action"] == "wait"


def test_a_wait_nobody_cancels_gives_up():
    p = pend("finish")
    assert rc.decide(p, RUNNING, now=T0 + timedelta(hours=13), process_started_at=None)["action"] == "fail"


def test_terminal_and_empty_are_none():
    assert rc.decide(None, DONE, now=T0, process_started_at=None) == {"action": "none"}
    for st in ("done", "failed", "cancelled"):
        assert rc.decide(pend(status=st), RUNNING, now=T0, process_started_at=None) == {"action": "none"}
    with pytest.raises(ValueError):
        rc.new_pending(edge="", mode="later", by="", reason="", restart=True, now=T0, release_id="x")
    assert "waiting" in rc.describe(pend(), RUNNING) and rc.describe(None, DONE) == "no release pending"
