"""A background window says what it left; a waiting job gains priority as it waits (19 Oct 2026).

vera-d2's board item bfa5efe9: the 03:30-05:00 window reported green with 23 jobs left - 20 shadow judges, default
priority, waiting 8.7 h with 0 attempts, while the window ran 80 other jobs. Strict priority starved them, and "green"
hid it.
"""
import os

import pytest

from vera import idle_queue as iq

pytestmark = pytest.mark.critical

H = 3600.0


def _job(i, prio, enq):
    return {"id": i, "state": iq.WAITING, "priority": prio, "enqueued_at": enq}


def test_an_old_default_job_overtakes_fresh_bulk_work():
    now = 100 * H
    jobs = [_job("judge", iq.DEFAULT_PRIORITY, now - 8.7 * H), _job("embed", 30, now - 0.1 * H)]
    assert [j["id"] for j in iq.pending(jobs, now)] == ["judge", "embed"]


def test_equal_waits_keep_their_priority_order_and_the_boost_is_capped():
    now = 100 * H
    jobs = [_job("bulk", 85, now - 50 * H), _job("narrator", 10, now - 50 * H)]
    assert [j["id"] for j in iq.pending(jobs, now)] == ["narrator", "bulk"]
    assert iq.effective_priority(_job("x", 50, 1), now) == 50 - iq.AGE_BOOST_MAX   # (an unknown enqueue time, 0, gets no boost)


def test_a_fresh_job_is_ordered_as_before():
    now = 100 * H
    jobs = [_job("b", 50, now - 1), _job("a", 20, now - 1)]
    assert [j["id"] for j in iq.pending(jobs, now)] == ["a", "b"]


def test_the_window_says_what_it_left_and_the_run_is_partial_not_green():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "schedule_capabilities.py"), encoding="utf-8").read()
    w = src[src.index("async def cap_background_window("):src.index('@capability("evolve.schedule.stop_run"')]
    assert '"partial": bool(left)' in w and '"left_by": by' in w and "the oldest has waited" in w
    assert 'if res.get("partial"):' in src and 'return "partial"' in src
