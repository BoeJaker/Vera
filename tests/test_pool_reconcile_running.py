"""A live container outranks a filesystem probe, and a pin must actually pin.

Measured on 2026-08-31 from evolve.audit.list. Two consecutive hourly prune
passes, 17 minutes apart, with ZERO worktrees reaped in between:

    21:41:40  sandbox.prune  ... | pool: nothing to reconcile (2 descriptor(s))
    21:58:30  sandbox.prune  ... 2 dead pool entr(ies), 2 reconciled (worktree-gone)

The same two descriptors went PRESENT -> ABSENT with nothing having changed on
disk. The heal path runs `docker rm -f <name>` before dropping the descriptor,
so both live containers were destroyed - the standing bleeding-edge mirror, and
another agent's feat/pwa-installable sandbox spawned 23 minutes earlier.

Why the probe flipped is still open (O37). This guards the decision that turned
an unreliable probe into destroyed work.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import sandbox_pool_reconcile as pr  # noqa: E402

pytestmark = pytest.mark.critical


MIRROR = {"name": "vera-dev-loop-lab-bleeding-edge-mirror",
          "worktree": "/home/boejaker/Vera/.loop-lab-worktrees/bleeding-edge-mirror"}
OTHER = {"name": "vera-dev-feat-pwa-installable",
         "worktree": "/home/boejaker/Vera/.loop-lab-worktrees/feat-pwa-installable"}
POOL = {"bleeding-edge-mirror": MIRROR, "feat-pwa-installable": OTHER}
BOTH_RUNNING = [MIRROR["name"], OTHER["name"]]
BOTH_ABSENT = {"bleeding-edge-mirror": pr.ABSENT, "feat-pwa-installable": pr.ABSENT}


def _plan(**kw):
    args = dict(container_names=BOTH_RUNNING, docker_ok=True,
                worktree_probe=BOTH_ABSENT, pinned_names=())
    args.update(kw)
    return pr.plan_pool_reconcile(POOL, **args)


# ── the 21:58 destruction ───────────────────────────────────────────────────

def test_a_running_container_is_not_reaped_on_an_absent_worktree():
    """The exact input of the 21:58 pass: both probed absent, both containers up."""
    plan = _plan()
    assert plan["heal"] == []
    assert set(plan["kept"]) == {"bleeding-edge-mirror", "feat-pwa-installable"}
    assert all(v == pr.KEEP_RUNNING for v in plan["kept"].values())


def test_the_kept_reason_says_why():
    """A silent keep is as hard to diagnose as a silent removal."""
    assert "running" in pr.KEEP_RUNNING
    assert pr.KEEP_RUNNING in _plan()["kept"].values()


def test_a_genuinely_dead_sandbox_is_still_reconciled():
    """The guard must not disable the feature: worktree gone AND no container
    is a half-alive descriptor holding a port, and still gets healed."""
    plan = pr.plan_pool_reconcile(
        {"bleeding-edge-mirror": MIRROR},
        container_names=["something-else"], docker_ok=True,
        worktree_probe={"bleeding-edge-mirror": pr.ABSENT})
    assert plan["heal"] == ["bleeding-edge-mirror"]


def test_a_present_worktree_with_no_container_is_still_stale():
    plan = pr.plan_pool_reconcile(
        {"bleeding-edge-mirror": MIRROR},
        container_names=["other"], docker_ok=True,
        worktree_probe={"bleeding-edge-mirror": pr.PRESENT})
    assert plan["stale"] == ["bleeding-edge-mirror"]


# ── the pin that never pinned ───────────────────────────────────────────────

def test_a_pinned_container_name_protects_the_descriptor():
    """`pinned` lives in a Redis SET of container names, never on the
    descriptor, so `d["pinned"]` was always falsy and the mirror - which IS in
    that set - was reaped three times."""
    plan = _plan(pinned_names=[MIRROR["name"]])
    assert plan["kept"]["bleeding-edge-mirror"] == pr.KEEP_PINNED


def test_a_pin_survives_even_a_dead_sandbox():
    """Standing infrastructure is never auto-reaped, which is what pinning is
    for - otherwise the pin only works while nothing is wrong."""
    plan = pr.plan_pool_reconcile(
        {"bleeding-edge-mirror": MIRROR},
        container_names=[], docker_ok=True,
        worktree_probe={"bleeding-edge-mirror": pr.ABSENT},
        pinned_names=[MIRROR["name"]])
    assert plan["heal"] == [] and plan["stale"] == []
    assert plan["kept"]["bleeding-edge-mirror"] == pr.KEEP_PINNED


def test_a_pin_by_slug_also_works():
    assert _plan(pinned_names=["bleeding-edge-mirror"])["kept"]["bleeding-edge-mirror"] == pr.KEEP_PINNED


def test_the_descriptor_flag_still_pins():
    d = dict(MIRROR); d["pinned"] = True
    plan = pr.plan_pool_reconcile({"m": d}, container_names=[], docker_ok=True,
                                  worktree_probe={"m": pr.ABSENT})
    assert plan["kept"]["m"] == pr.KEEP_PINNED


def test_blank_pins_are_ignored():
    plan = _plan(pinned_names=["", "   ", None])
    assert all(v == pr.KEEP_RUNNING for v in plan["kept"].values())


# ── ordering: unobservable docker must decide nothing ───────────────────────

def test_docker_unobservable_beats_an_absent_worktree():
    """The ABSENT branch force-removes a container, so it must never be reached
    while docker cannot be seen - previously it was evaluated first."""
    plan = _plan(docker_ok=False)
    assert plan["heal"] == []
    assert all(v == pr.KEEP_DOCKER_BLIND for v in plan["kept"].values())


def test_an_empty_container_list_with_a_full_pool_is_unobservable():
    plan = _plan(container_names=[])
    assert plan["heal"] == [] and plan["stale"] == []
    assert plan["docker_observable"] is False


def test_an_unprobed_slug_is_never_deleted():
    plan = _plan(worktree_probe={})
    assert plan["heal"] == []


# ── the call site ───────────────────────────────────────────────────────────

def test_prune_passes_the_pinned_set():
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "vera", "evolve", "evolve_capabilities.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "pinned_names=await _sandbox_pinned()" in src
