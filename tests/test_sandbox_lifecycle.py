import pytest

from vera.evolve.sandbox_lifecycle import (
    classify_sandbox,
    lifecycle_preflight,
    primary_replacement_conflict,
    resolve_restart_target,
)


pytestmark = pytest.mark.critical


def test_primary_guard_refuses_a_different_branch_before_replacement():
    conflict = primary_replacement_conflict(
        {"branch": "feat/owned", "worktree": "/safe/owned"},
        "feat/incoming", "running")
    assert conflict == {
        "error": "primary sandbox is occupied by another branch",
        "code": "primary_occupied",
        "current_branch": "feat/owned",
        "requested_branch": "feat/incoming",
        "container_status": "running",
        "hint": ("use evolve.sandbox.spawn for an additive sandbox; only pass "
                 "replace_primary=true when the current owner has explicitly released it"),
    }


@pytest.mark.parametrize("current,requested,status,replace", [
    ({"branch": "feat/same"}, "feat/same", "running", False),
    ({"branch": "feat/old"}, "feat/new", "running", True),
    ({"branch": "feat/stale"}, "feat/new", "", False),
    ({}, "feat/new", "", False),
])
def test_primary_guard_allows_only_non_replacement_or_explicit_replacement(
        current, requested, status, replace):
    assert primary_replacement_conflict(current, requested, status, replace) is None


def test_primary_guard_fails_closed_when_container_owner_is_unknown():
    conflict = primary_replacement_conflict({}, "feat/incoming", "running")
    assert conflict["code"] == "primary_occupied"
    assert conflict["current_branch"] == "(unknown)"
    assert conflict["container_status"] == "running"


def test_restart_resolution_preserves_primary_role_and_descriptor():
    primary = {"branch": "feat/primary", "port": 8998, "redis_db": 3,
               "worktree": "/wt/primary", "role": "stale", "name": "wrong"}
    result = resolve_restart_target(primary, {}, primary_name="vera-dev",
                                    branch="feat/primary")
    assert result == {**primary, "role": "primary", "name": "vera-dev"}


def test_restart_resolution_preserves_spawned_role_and_descriptor():
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "port": 8995, "redis_db": 4, "worktree": "/wt/spawned",
               "role": "stale"}
    result = resolve_restart_target(
        {"branch": "feat/primary"}, {"feat-spawned": spawned},
        primary_name="vera-dev", branch="feat/spawned")
    assert result == {**spawned, "role": "spawned", "slug": "feat-spawned"}


def test_restart_resolution_never_falls_back_to_primary_for_unknown_branch():
    result = resolve_restart_target(
        {"branch": "feat/primary"}, {}, primary_name="vera-dev",
        branch="feat/missing")
    assert result["code"] == "sandbox_not_found"
    assert "feat/missing" in result["error"]


@pytest.mark.parametrize("observable,status,worktree,expected", [
    (False, "", True, "docker_unreachable"),
    (True, "running", True, "healthy"),
    (True, "paused", True, "paused"),
    (True, "exited", True, "exited"),
    (True, "", True, "stale_descriptor"),
    (True, "running", False, "missing_worktree"),
])
def test_sandbox_state_classification(observable, status, worktree, expected):
    assert classify_sandbox(docker_observable=observable,
                            container_status=status,
                            worktree_exists=worktree) == expected


def test_preflight_restart_is_allowed_only_with_observable_container_and_worktree():
    allowed = lifecycle_preflight(
        {"branch": "feat/safe"}, action="restart", docker_observable=True,
        container_status="paused", worktree_exists=True, dirty=True,
        merged_to_bleeding_edge=False)
    assert allowed["allowed"] is True
    assert allowed["dry_run"] is True and allowed["mutated"] is False

    refused = lifecycle_preflight(
        {"branch": "feat/safe"}, action="restart", docker_observable=False,
        container_status="", worktree_exists=True, dirty=False,
        merged_to_bleeding_edge=True)
    assert refused["allowed"] is False
    assert "docker_state_unknown" in refused["reasons"]


def test_destructive_preflight_fails_closed_for_dirty_unmerged_or_protected_work():
    dirty = lifecycle_preflight(
        {"branch": "feat/wip"}, action="remove", docker_observable=True,
        container_status="exited", worktree_exists=True, dirty=True,
        merged_to_bleeding_edge=False)
    assert dirty["allowed"] is False
    assert "worktree_not_proven_clean" in dirty["reasons"]
    assert "branch_not_proven_merged" in dirty["reasons"]
    assert "container_still_present" in dirty["reasons"]

    standing = lifecycle_preflight(
        {"branch": "loop-lab/bleeding-edge-mirror", "pinned": True},
        action="reconcile", docker_observable=True, container_status="",
        worktree_exists=True, dirty=False, merged_to_bleeding_edge=True)
    assert standing["allowed"] is False
    assert "protected_sandbox" in standing["reasons"]


def test_reconcile_is_only_planned_for_clean_merged_stale_descriptor():
    decision = lifecycle_preflight(
        {"branch": "feat/landed"}, action="reconcile", docker_observable=True,
        container_status="", worktree_exists=True, dirty=False,
        merged_to_bleeding_edge=True)
    assert decision["allowed"] is True
    assert decision["state"] == "stale_descriptor"
    assert decision["mutated"] is False
