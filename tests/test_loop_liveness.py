"""A loop inside one long call is working, not dead.

Census 23, build-browser-verified:

    t+ 177s   operator.run called
    t+1381s   reported "interrupted" - the census recorded it and moved on
    t+1522s   operator.run returned (1,344,677 ms)
    t+7522s   agent_loop_v6.done - it had been working the whole time

The abandoned loop then held the GPU for every goal that followed, so the run's
later wall times were measured under contention it caused.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import loop_liveness as ll  # noqa: E402

pytestmark = pytest.mark.critical

#: The live values: OLLAMA_GEN_TIMEOUT 900 -> max(600, 900+300).
WINDOW = 1200.0
#: The call that caused it.
OPERATOR_CALL_S = 1344.677


# ── why the old signal failed ───────────────────────────────────────────────

def test_the_operator_call_outlasts_the_stale_window():
    """The premise. The window is derived from ONE generation; operator.run is a
    composite call that makes up to max_steps of them."""
    assert ll.stale_secs(900) == WINDOW
    assert OPERATOR_CALL_S > WINDOW


def test_a_run_quiet_for_that_long_reads_as_stale():
    """Which is exactly what happened at t+1381s."""
    assert ll.is_stale(1381, WINDOW) is True


def test_the_heartbeat_keeps_the_marker_far_inside_the_window():
    """With a beat every interval, the marker's age never approaches the window,
    so the length of a tool call stops mattering at all."""
    iv = ll.heartbeat_interval(WINDOW)
    assert iv < WINDOW / 3
    assert ll.is_stale(iv, WINDOW) is False


def test_two_missed_beats_still_leave_the_run_alive():
    """One dropped write - a busy event loop, a Redis blip - must not resurrect
    the bug."""
    assert ll.survives_missed_beats(WINDOW, missed=2) is True


def test_the_beat_rate_buys_resilience_at_any_window():
    """BEATS_PER_WINDOW is the constant that makes a lost write survivable. At
    1200s the MAX_INTERVAL clamp happens to hide a bad value, so this checks a
    window small enough for the ratio itself to decide."""
    for w in (60.0, 120.0, 300.0, 1200.0):
        assert ll.survives_missed_beats(w, missed=2) is True, w


def test_the_interval_is_bounded_at_both_ends():
    assert ll.heartbeat_interval(4) == ll.MIN_INTERVAL_S      # tiny window
    assert ll.heartbeat_interval(10 ** 6) == ll.MAX_INTERVAL_S  # absurd window
    assert ll.heartbeat_interval(0) == ll.MIN_INTERVAL_S


def test_a_missing_marker_is_not_staleness():
    """Absence of information is not evidence of death - deciding "dead" from "I
    have no marker" is what turned a working loop into a recorded failure."""
    assert ll.is_stale(None, WINDOW) is False


def test_a_genuinely_old_marker_is_still_stale():
    """The check must keep working for the case it exists for - a restarted
    server leaves no heartbeat, so the marker ages out and the run is correctly
    reported interrupted."""
    assert ll.is_stale(WINDOW + 1, WINDOW) is True


def test_junk_does_not_raise():
    assert ll.is_stale("x", WINDOW) is False
    assert ll.stale_secs(None) == WINDOW
    assert ll.heartbeat_interval("x") > 0


# ── the wiring ──────────────────────────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(os.path.dirname(__file__), "..", *parts),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_heartbeat_is_tied_to_the_runner_task():
    """Not to the loop's progress: it must stop when the task does, or a dead
    run would be kept looking alive for ever."""
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    body = src[src.index("async def _loop_heartbeat("):]
    body = body[:body.index("\ndef ", 10)] if "\ndef " in body[10:] else body[:3000]
    assert "while task is not None and not task.done():" in body


def test_the_heartbeat_writes_the_same_marker_the_check_reads():
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    assert 'zadd("vera:loop:sessions"' in src
    assert 'zscore("vera:loop:sessions"' in src


def test_the_heartbeat_starts_where_the_task_registers():
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    reg = src[src.index("def _register_loop_task("):]
    reg = reg[:reg.index("async def _loop_run_is_stale")]
    assert "_LOOP_HEARTBEATS[sid] = asyncio.create_task(_loop_heartbeat(sid, task))" in reg


def test_the_heartbeat_is_cancelled_with_the_task():
    """Otherwise every finished loop leaves a timer running for the life of the
    process."""
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    reg = src[src.index("def _register_loop_task("):]
    reg = reg[:reg.index("async def _loop_run_is_stale")]
    drop = reg[reg.index("def _drop("):]
    assert "hb.cancel()" in drop


def test_the_interval_is_derived_from_the_window_not_guessed():
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    assert "_loop_liveness.heartbeat_interval(_LOOP_STALE_SECS)" in src


def test_a_failed_import_leaves_the_old_behaviour():
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    assert "_loop_liveness = None" in src
    assert "if _loop_liveness is not None and sid not in _LOOP_HEARTBEATS:" in src
