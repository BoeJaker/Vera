"""Worktree CLAIMS — an agent declaring "I am still working here".

The gap these close: protection was only ever available to worktrees registered
as sandboxes (the primary, or a pool entry). A worktree an agent created by hand
— increasingly the norm, because the sandbox pool runs out of slots — had no way
to say it was in use. The moment its branch was promoted it had 0 unique commits
vs base, so **the act of landing work marked the worktree you were still working
in as disposable**, and the next sweep removed it mid-use. Because the directory
is usually held open by a container bind-mount, the removal left it SEVERED
(directory + .git file present, admin entry gone) rather than cleanly deleted,
which presents as repo corruption rather than as cleanup.

Inferring "in use" from file mtimes was considered and rejected: it is noisy in
both directions (a container writing .pyc fakes activity; an agent thinking for
an hour looks idle). A claim is the owner stating the fact.
"""

import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from vera.evolve.sandbox_reap import plan_reap  # noqa: E402


WT = ".loop-lab-worktrees"


def _wt(name, branch):
    return {"path": f"/home/u/Vera/{WT}/{name}", "branch": branch, "is_main": False}


def _paths(bucket):
    return {e["path"] for e in bucket}


@pytest.mark.critical
def test_claim_survives_its_branch_being_merged():
    """THE regression: promoting your work must not mark your worktree disposable."""
    w = _wt("my-work", "feat/x")
    plan = plan_reap(
        worktrees=[w],
        merged_branches=["feat/x"],          # promote just merged it
        claimed_paths={w["path"]: "claude"},
        base_branch="bleeding-edge",
    )
    assert _paths(plan["reap"]) == set(), "a claimed worktree must never be reaped"
    assert _paths(plan["keep"]) == {w["path"]}
    assert "claude" in plan["keep"][0]["reason"]


@pytest.mark.critical
def test_without_a_claim_a_merged_worktree_is_still_reapable():
    """The claim must be what changes the outcome — otherwise it proves nothing."""
    w = _wt("stale", "feat/old")
    plan = plan_reap(worktrees=[w], merged_branches=["feat/old"],
                     base_branch="bleeding-edge")
    assert _paths(plan["reap"]) == {w["path"]}


@pytest.mark.critical
def test_expired_claim_goes_to_review_never_reap():
    """A lapsed claim means nobody is asserting ownership — not that the work is
    disposable. It must surface for a decision, not be deleted."""
    w = _wt("abandoned", "feat/y")
    plan = plan_reap(
        worktrees=[w],
        merged_branches=["feat/y"],
        expired_claim_paths={w["path"]: "codex"},
        base_branch="bleeding-edge",
    )
    assert _paths(plan["reap"]) == set()
    assert _paths(plan["review"]) == {w["path"]}
    assert "codex" in plan["review"][0]["reason"]


@pytest.mark.critical
def test_a_live_claim_outranks_an_expired_one_for_the_same_path():
    """Refreshing a claim must actually take effect."""
    w = _wt("refreshed", "feat/z")
    plan = plan_reap(
        worktrees=[w],
        merged_branches=["feat/z"],
        claimed_paths={w["path"]: "claude"},
        expired_claim_paths={w["path"]: "claude"},
        base_branch="bleeding-edge",
    )
    assert _paths(plan["keep"]) == {w["path"]}
    assert _paths(plan["reap"]) == set()


@pytest.mark.critical
def test_claims_do_not_weaken_any_existing_guard():
    """Claims are additive. Everything protected before must stay protected, and
    unmerged work must still never be auto-removed."""
    main = {"path": "/home/u/Vera", "branch": "main", "is_main": True}
    live = _wt("has-container", "feat/live")
    trunk = _wt("mirror", "loop-lab/bleeding-edge-mirror")
    dirty = _wt("wip", "feat/dirty")
    unmerged = _wt("unlanded", "feat/unmerged")
    plan = plan_reap(
        worktrees=[main, live, trunk, dirty, unmerged],
        protected_paths=[live["path"]],
        dirty_paths=[dirty["path"]],
        merged_branches=["feat/live", "loop-lab/bleeding-edge-mirror", "feat/dirty"],
        claimed_paths={},
        base_branch="bleeding-edge",
    )
    assert _paths(plan["reap"]) == set()
    assert _paths(plan["keep"]) == {main["path"], live["path"], trunk["path"]}
    assert _paths(plan["review"]) == {dirty["path"], unmerged["path"]}


@pytest.mark.critical
def test_claim_paths_are_matched_regardless_of_trailing_slash_or_separator():
    """Claims arrive from callers on different platforms; a path mismatch would
    silently drop the protection, which is the failure this exists to prevent."""
    w = _wt("sep", "feat/s")
    for variant in (w["path"] + "/", w["path"].replace("/", "\\")):
        plan = plan_reap(worktrees=[w], merged_branches=["feat/s"],
                         claimed_paths={variant: "claude"},
                         base_branch="bleeding-edge")
        assert _paths(plan["reap"]) == set(), f"claim not matched for {variant!r}"


@pytest.mark.critical
def test_no_claims_argument_behaves_exactly_as_before():
    """Callers that never pass claims must be unaffected."""
    w = _wt("plain", "feat/p")
    before = plan_reap(worktrees=[w], merged_branches=["feat/p"], base_branch="x")
    after = plan_reap(worktrees=[w], merged_branches=["feat/p"], base_branch="x",
                      claimed_paths=None, expired_claim_paths=None)
    assert before == after
    assert _paths(before["reap"]) == {w["path"]}
