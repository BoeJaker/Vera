"""A read that failed must never be read as "it is gone".

`sandbox.prune` decided two destructive reaps from unchecked reads. `_sh`
returns {"ok": False, "out": ""} for a missing binary, a 240s timeout or any
exception, so a FAILED `docker ps -a` looked exactly like "no containers
exist" - and every registered sandbox was then classified "container TRULY
removed" and deleted. Observed 2026-08-30: 13 registrations to 0 in one pass,
and the pinned standing mirror repeatedly de-registered while its worktree was
demonstrably present. Losing a registration kills `evolve.sandbox.exec`, the
only host-exec route agents have.

These tests fix the asymmetry in the decision: waiting costs a stale descriptor
until the next sweep, acting costs the estate.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.evolve.sandbox_pool_reconcile import (      # noqa: E402
    ABSENT, PRESENT, UNKNOWN, audit_summary, docker_is_observable,
    plan_pool_reconcile,
)

MIRROR = {"name": "vera-dev-mirror", "worktree": "/wt/mirror", "pinned": True}
WORKER = {"name": "vera-dev-worker", "worktree": "/wt/worker"}
OTHER = {"name": "vera-dev-other", "worktree": "/wt/other"}
POOL = {"mirror": MIRROR, "worker": WORKER, "other": OTHER}
ALL_PRESENT = {"mirror": PRESENT, "worker": PRESENT, "other": PRESENT}


def _plan(pool=POOL, names=("vera-dev-mirror", "vera-dev-worker", "vera-dev-other"),
          ok=True, probe=None):
    return plan_pool_reconcile(pool, container_names=names, docker_ok=ok,
                               worktree_probe=ALL_PRESENT if probe is None else probe)


# --- the estate-wipe -------------------------------------------------------

def test_a_failed_docker_read_reaps_nothing():
    """The 13-to-0 case: rc!=0 means unknown, not empty."""
    plan = _plan(names=(), ok=False)
    assert plan["stale"] == [] and plan["heal"] == []
    assert plan["docker_observable"] is False
    assert set(plan["kept"]) == {"mirror", "worker", "other"}


def test_a_successful_but_empty_listing_reaps_nothing_while_the_pool_is_full():
    """"Zero containers" alongside live descriptors is a broken read, not news."""
    plan = _plan(names=(), ok=True)
    assert plan["stale"] == []
    assert plan["docker_observable"] is False


def test_an_empty_pool_with_an_empty_listing_is_not_an_error():
    plan = plan_pool_reconcile({}, container_names=(), docker_ok=True,
                               worktree_probe={})
    assert plan["stale"] == [] and plan["heal"] == []
    assert plan["docker_observable"] is True


def test_a_genuinely_missing_container_is_still_reaped():
    """The guard must not disable the cleanup it is guarding."""
    plan = _plan(names=("vera-dev-mirror", "vera-dev-worker"))
    assert plan["stale"] == ["other"]


# --- pinned ----------------------------------------------------------------

def test_a_pinned_sandbox_is_never_reaped_even_when_its_container_is_missing():
    """`pinned` is documented as protecting the standing mirror; it did not."""
    plan = _plan(names=("vera-dev-worker", "vera-dev-other"))
    assert "mirror" not in plan["stale"]
    assert "pinned" in plan["kept"]["mirror"]


def test_a_pinned_sandbox_is_never_healed_even_when_its_worktree_looks_absent():
    plan = _plan(probe={"mirror": ABSENT, "worker": PRESENT, "other": PRESENT})
    assert plan["heal"] == []


# --- the worktree probe ----------------------------------------------------

def test_a_worktree_proven_absent_is_reconciled():
    plan = _plan(probe={"mirror": PRESENT, "worker": ABSENT, "other": PRESENT})
    assert plan["heal"] == ["worker"]


def test_a_worktree_that_could_not_be_probed_is_never_reconciled():
    """"I could not look" is not "it is not there" - Path.exists() conflated them."""
    plan = _plan(probe={"mirror": PRESENT, "worker": UNKNOWN, "other": PRESENT})
    assert plan["heal"] == []
    assert "could not be probed" in plan["kept"]["worker"]


def test_a_slug_absent_from_the_probe_defaults_to_unknown():
    """A caller that forgets to probe must not thereby delete something."""
    plan = _plan(probe={})
    assert plan["heal"] == []


def test_a_descriptor_with_no_worktree_is_still_subject_to_the_container_check():
    pool = {"nowt": {"name": "vera-dev-nowt"}}
    assert plan_pool_reconcile(pool, container_names=("something-else",),
                               docker_ok=True, worktree_probe={})["stale"] == ["nowt"]


# --- observability predicate -----------------------------------------------

@pytest.mark.parametrize("ok,names,size,expected", [
    (True, ["a"], 1, True),
    (False, ["a"], 1, False),      # command failed
    (True, [], 3, False),          # empty read while descriptors exist
    (True, [], 0, True),           # genuinely nothing anywhere
])
def test_docker_is_observable(ok, names, size, expected):
    assert docker_is_observable(ok=ok, container_names=names, pool_size=size) is expected


# --- the audit line --------------------------------------------------------

def test_the_audit_line_names_what_it_acted_on():
    """Counts alone are why three occurrences in one session were indistinguishable."""
    plan = _plan(names=("vera-dev-mirror", "vera-dev-worker"))
    line = audit_summary(plan, POOL)
    assert "other" in line


def test_the_audit_line_says_when_it_refused_to_act():
    line = audit_summary(_plan(names=(), ok=False), POOL)
    assert "unobservable" in line
    assert "untouched" in line


# --- the callsite ----------------------------------------------------------

def test_the_prune_no_longer_decides_reaps_from_an_unchecked_read():
    """Shape guard: the pure core is only a fix if the prune actually uses it."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve",
                            "evolve_capabilities.py"), encoding="utf-8").read()
    assert "_pool_reconcile.plan_pool_reconcile(" in src
    assert "docker_ok=bool(ps.get(\"ok\"))" in src, "the ok flag must be consulted"
    assert "if not Path(wt).exists():\n                heal_pool.append" not in src
