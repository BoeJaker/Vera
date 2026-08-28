"""A routing slot whose request never returned must be reclaimed.

Observed 2026-08-28: two loop runs stalled at their first LLM call. Afterwards
the router reported in_use=2 while there were NO loop sessions, the GPU gate was
free, and every model on every node had passed its keep-alive expiry - Ollama was
doing nothing. The requests had been issued and never came back, so the `finally`
that releases the slot never ran.

Cancelling the loop does not fix it: the cancel flag is only observed BETWEEN
awaits, and a coroutine blocked on an HTTP read never reaches one. Nothing else
reconciled the counter, so pick_instance kept routing around idle nodes, and the
next census goal stalled the same way.

Pure - no app import, no cluster.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.ollama_inflight import (          # noqa: E402
    DEFAULT_GRACE_S, max_age_for, reconcile, stale_ids,
)

NOW = 10_000.0


def test_a_slot_held_past_the_bound_is_reclaimed():
    inflight = {"stuck": NOW - 5000.0}
    new_in_use, reclaimed = reconcile(1, inflight, NOW, max_age_s=1000.0)
    assert reclaimed == ["stuck"]
    assert new_in_use == 0
    assert inflight == {}, "the reclaimed slot must be dropped, not just counted"


def test_a_live_request_is_never_stolen():
    """Reclaiming a slot that is still working is the failure this must not cause."""
    inflight = {"live": NOW - 10.0}
    new_in_use, reclaimed = reconcile(1, inflight, NOW, max_age_s=1000.0)
    assert reclaimed == [] and new_in_use == 1
    assert "live" in inflight


def test_the_counter_is_decreased_by_exactly_what_was_reclaimed():
    """Media slots share in_use, so it must be adjusted - never recomputed."""
    inflight = {"a": NOW - 5000.0, "b": NOW - 5000.0}
    new_in_use, reclaimed = reconcile(5, inflight, NOW, max_age_s=1000.0)
    assert len(reclaimed) == 2
    assert new_in_use == 3, "the 3 slots held by other subsystems must survive"


def test_it_never_goes_negative():
    inflight = {"a": NOW - 5000.0, "b": NOW - 5000.0}
    new_in_use, _ = reconcile(1, inflight, NOW, max_age_s=1000.0)
    assert new_in_use == 0


def test_a_clock_oddity_does_not_cause_a_reclaim():
    """A future or unparseable start time must read as fresh, not as ancient."""
    inflight = {"future": NOW + 500.0, "junk": "not-a-number", "none": None}
    _, reclaimed = reconcile(3, inflight, NOW, max_age_s=10.0)
    assert reclaimed == [], "no slot should be reclaimed on a bad timestamp"


def test_empty_and_disabled_cases_are_safe():
    assert stale_ids({}, NOW, 100.0) == []
    assert stale_ids({"a": 0.0}, NOW, 0) == [], "max_age<=0 disables the sweep"
    assert reconcile(0, {}, NOW, 100.0) == (0, [])


def test_the_bound_exceeds_the_generation_timeout():
    """A legitimately long generation must finish before its slot is presumed lost."""
    assert max_age_for(900.0) == 900.0 + DEFAULT_GRACE_S
    assert max_age_for(900.0) > 900.0


def test_the_bound_has_a_floor_for_a_missing_timeout():
    for bad in (0, None, "", -5):
        assert max_age_for(bad) >= 60.0, bad


def test_reconcile_is_idempotent():
    """Sweeping twice must not double-subtract."""
    inflight = {"stuck": NOW - 5000.0}
    first, _ = reconcile(2, inflight, NOW, max_age_s=1000.0)
    second, reclaimed2 = reconcile(first, inflight, NOW, max_age_s=1000.0)
    assert reclaimed2 == [] and second == first
