import pytest

from vera.evolve.sandbox_lifecycle import (
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
