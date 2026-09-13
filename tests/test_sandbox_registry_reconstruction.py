import copy

import pytest

from vera.evolve.sandbox_registry_reconstruction import (
    compose_recovery_labels,
    descriptors_for_apply,
    plan_registry_reconstruction,
)


def test_compose_recovery_labels_preserve_branch_and_allocated_slot():
    assert compose_recovery_labels("feat/safe-work", 6) == {
        "vera.loop-lab.role": "spawned",
        "vera.loop-lab.branch": "feat/safe-work",
        "vera.loop-lab.redis-db": "6",
    }
    with pytest.raises(ValueError, match="outside"):
        compose_recovery_labels("feat/safe-work", 99)


def observation(**changes):
    value = {
        "name": "vera-dev-feat-safe-work",
        "status": "paused",
        "labels": {
            "com.docker.compose.service": "vera-dev-feat-safe-work",
            "com.docker.compose.project": "vera-dev-feat-safe-work",
            "com.docker.compose.project.working_dir": "/srv/Vera",
            "com.docker.compose.project.config_files":
                "/srv/Vera/docker-compose.yml,/srv/Vera/docker-compose.dev-feat-safe-work.yml",
            "vera.loop-lab.role": "spawned",
            "vera.loop-lab.branch": "feat/safe-work",
            "vera.loop-lab.redis-db": "6",
        },
        "env": {"VERA_IS_DEV_SANDBOX": "1", "REDIS_URL": "redis://sidecar:6379/6"},
        "mounts": {"/app/Vera": "/srv/Vera/.loop-lab-worktrees/feat-safe-work"},
        "published_ports": [8992],
        "gate_token_sha256": "a" * 64,
    }
    value.update(changes)
    return value


def make_plan(observations=None, existing=None):
    return plan_registry_reconstruction(
        observations if observations is not None else [observation()],
        worktrees=[{"path": "/srv/Vera/.loop-lab-worktrees/feat-safe-work",
                    "branch": "feat/safe-work"}],
        existing_pool=existing or {}, repo_root="/srv/Vera",
        existing_compose_files=["docker-compose.dev-feat-safe-work.yml"],
    )


@pytest.mark.critical
def test_exact_observation_produces_payload_free_restore_descriptor():
    plan = make_plan()
    assert plan["apply_allowed"] is True
    assert plan["blocked"] == []
    descriptor = plan["eligible"][0]["descriptor"]
    assert descriptor == {
        "branch": "feat/safe-work", "slug": "feat-safe-work",
        "name": "vera-dev-feat-safe-work", "port": 8992, "redis_db": 6,
        "compose": "docker-compose.dev-feat-safe-work.yml",
        "worktree": "/srv/Vera/.loop-lab-worktrees/feat-safe-work",
        "owner": "unknown", "session_id": "",
        "ownership_source": "restored_from_observation",
        "gate_token_sha256": "a" * 64,
    }
    assert descriptors_for_apply(plan, plan["digest"])["feat-safe-work"] == descriptor


@pytest.mark.critical
@pytest.mark.parametrize("mutation,reason", [
    (lambda value: value["labels"].update({"com.docker.compose.service": "redis"}),
     "compose_service_mismatch"),
    (lambda value: value.update({"status": ""}), "container_state_unverified"),
    (lambda value: value["labels"].update({"com.docker.compose.project": "other"}),
     "compose_project_mismatch"),
    (lambda value: value["labels"].pop("vera.loop-lab.role"),
     "sandbox_role_label_missing"),
    (lambda value: value["labels"].update({"vera.loop-lab.branch": "feat/other"}),
     "branch_label_mismatch"),
    (lambda value: value["labels"].update({"com.docker.compose.project.working_dir": "/tmp"}),
     "compose_working_directory_mismatch"),
    (lambda value: value["mounts"].clear(), "vera_worktree_mount_missing"),
    (lambda value: value.update({"published_ports": [8991, 8992]}),
     "published_port_unverified"),
    (lambda value: value["labels"].update({"vera.loop-lab.redis-db": "99"}),
     "redis_db_unverified"),
    (lambda value: value["env"].pop("VERA_IS_DEV_SANDBOX"), "sandbox_marker_missing"),
    (lambda value: value.update({"gate_token_sha256": ""}),
     "gate_token_identity_unverified"),
])
def test_each_required_evidence_source_fails_closed(mutation, reason):
    item = observation()
    mutation(item)
    plan = make_plan([item])
    assert plan["eligible"] == []
    assert reason in plan["blocked"][0]["reasons"]


@pytest.mark.critical
def test_existing_descriptor_is_never_overwritten():
    existing = {"feat-safe-work": {"slug": "feat-safe-work", "name": "occupied",
                                    "branch": "feat/other", "port": 8990,
                                    "redis_db": 8, "worktree": "/other"}}
    plan = make_plan(existing=existing)
    assert plan["apply_allowed"] is False
    assert "existing_slug_claim" in plan["blocked"][0]["reasons"]


@pytest.mark.critical
def test_duplicate_observations_block_the_entire_ambiguous_batch():
    plan = make_plan([observation(), copy.deepcopy(observation())])
    assert plan["eligible"] == []
    assert len(plan["blocked"]) == 2
    assert all(item["reasons"] == ["duplicate_resource_claim"] for item in plan["blocked"])


@pytest.mark.critical
def test_apply_requires_the_exact_reviewed_digest_and_nonempty_plan():
    plan = make_plan()
    with pytest.raises(ValueError, match="changed"):
        descriptors_for_apply(plan, "0" * 64)
    empty = make_plan([])
    with pytest.raises(ValueError, match="no eligible"):
        descriptors_for_apply(empty, empty["digest"])
