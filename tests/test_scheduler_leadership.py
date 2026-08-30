"""Sweeps that mutate shared state must run in exactly one instance.

Seventeen `capability_orchestration` processes were found alive on one host on
2026-08-31 - fourteen holding no listening port, between five and twelve days
old - because the restart helper kills whatever holds port 8999 and nothing
else. `scheduler_loop` had no cross-instance gate, so every one of them ran
every ambient sweep against the single shared Redis.

That is how a fixed bug came back: a `sandbox.prune` emitted the PRE-fix audit
text minutes after the fixed build was serving :8999. A zombie ran the old
destructive sweep and deleted the pool descriptors the running build had just
been taught to protect.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.scheduler_leadership import (          # noqa: E402
    LEASE_TTL_S, RENEW_EVERY_S, lease_decision, may_run, should_check_lease,
)

ME = "hostA:100"
OTHER = "hostA:999"


# --- who may run ------------------------------------------------------------

def test_an_unclaimed_lease_is_taken():
    d = lease_decision(now=1000.0, me=ME, holder=None, expires_at=None)
    assert d["run"] is True and d["claim"] is True


def test_the_holder_keeps_running_and_renews():
    d = lease_decision(now=1000.0, me=ME, holder=ME, expires_at=1050.0)
    assert d["run"] is True and d["claim"] is True
    assert "renew" in d["reason"]


def test_another_live_holder_locks_this_instance_out():
    """The whole point: fourteen zombies must not run the destructive sweeps."""
    d = lease_decision(now=1000.0, me=ME, holder=OTHER, expires_at=1050.0)
    assert d["run"] is False and d["claim"] is False
    assert OTHER in d["reason"]


def test_an_expired_lease_is_taken_over():
    """A crashed leader must not wedge the sweeps forever."""
    d = lease_decision(now=1100.0, me=ME, holder=OTHER, expires_at=1050.0)
    assert d["run"] is True and d["claim"] is True
    assert "expired" in d["reason"]


def test_the_boundary_is_not_a_dead_zone():
    """Exactly at expiry the lease is takeable, not ambiguous."""
    assert lease_decision(now=1050.0, me=ME, holder=OTHER,
                          expires_at=1050.0)["run"] is True


def test_a_holder_with_no_expiry_is_not_trusted_forever():
    """A malformed record must not lock everyone out permanently."""
    d = lease_decision(now=1000.0, me=ME, holder=OTHER, expires_at=None)
    assert d["run"] is True and d["claim"] is True


# --- which jobs are affected ------------------------------------------------

def test_an_ordinary_job_is_never_gated():
    """This must not quietly stop per-instance housekeeping."""
    assert may_run({"name": "x"}, is_leader=False) is True
    assert may_run({"name": "x", "singleton": False}, is_leader=False) is True


def test_a_singleton_job_runs_only_in_the_leader():
    task = {"name": "evolve.sandbox.idle_sweep", "singleton": True}
    assert may_run(task, is_leader=True) is True
    assert may_run(task, is_leader=False) is False


# --- lease checking cadence -------------------------------------------------

def test_the_first_tick_always_checks():
    assert should_check_lease(now=1.0, last_checked=None) is True


def test_the_lease_is_not_checked_every_second():
    """The loop ticks 86400 times a day; the lease changes almost never."""
    assert should_check_lease(now=1000.0, last_checked=999.0) is False


def test_it_is_rechecked_once_the_renew_interval_passes():
    assert should_check_lease(now=1000.0 + RENEW_EVERY_S, last_checked=1000.0) is True


def test_renewal_happens_comfortably_inside_the_ttl():
    """Otherwise an ordinary slow tick silently costs leadership."""
    assert RENEW_EVERY_S * 2 < LEASE_TTL_S


# --- the callsite -----------------------------------------------------------

def _orch_src():
    with open(os.path.join(os.path.dirname(__file__), "..", "vera",
                           "capability_orchestration.py"), encoding="utf-8") as fh:
        return fh.read()


def test_the_scheduler_actually_consults_the_gate():
    """A pure core is only a fix if the loop uses it."""
    src = _orch_src()
    assert "_lead.may_run(task" in src
    assert "_refresh_scheduler_leadership()" in src


def test_the_lease_lives_on_the_shared_coordinator():
    """A per-instance Redis db would make every instance its own leader."""
    src = _orch_src()
    assert "await _ensure_coord_redis()" in src
    assert "COORD_REDIS or REDIS" in src


@pytest.mark.parametrize("job", [
    "evolve.sandbox.idle_sweep",
    "evolve.scaffolding.sweep",
    "evolve.mainline_mirror.refresh",
])
def test_the_destructive_sweeps_are_marked_singleton(job):
    """These pause/remove containers, worktrees and branches every instance
    shares. Running them fifteen times concurrently is what caused O26."""
    with open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve",
                           "evolve_capabilities.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.index(f'name="{job}"')
    window = src[max(0, i - 300):i + 300]
    assert "singleton=True" in window, f"{job} is not marked singleton"
