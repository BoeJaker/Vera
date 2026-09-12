from pathlib import Path

import pytest

from vera.inventory.execution_authority_review import (
    CURRENT_SURFACES,
    ExecutionAuthoritySurface,
    build_execution_authority_review,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_current_manifest_is_bound_to_real_source_and_non_executing():
    result = build_execution_authority_review(ROOT)
    assert result["schema"] == "vera.execution-authority-review/v1"
    assert result["removal_authority"] is False
    assert result["executes"] is False and result["mutates"] is False
    assert len(result["surfaces"]) == len(CURRENT_SURFACES)
    assert all(item["source_content_digest"].startswith("sha256:")
               for item in result["source_evidence"])


def test_review_distinguishes_native_schedulers_and_projection_transitions():
    result = build_execution_authority_review(ROOT)
    schedulers = [item for item in result["surfaces"]
                  if item["family"] == "scheduler"]
    assert {item["semantic_scope"] for item in schedulers} == {
        "process_interval_callbacks", "calendar_actions", "dream_triggers",
        "research_iterations",
    }
    lifecycle = next(item for item in result["surfaces"]
                     if item["name"] == "schedule.lifecycle_projection")
    assert lifecycle["role"] == "projection" and lifecycle["executes"] is False


def test_duplicate_idle_queue_state_gateways_are_the_only_structural_overlap():
    result = build_execution_authority_review(ROOT)
    assert len(result["structural_overlaps"]) == 1
    overlap = result["structural_overlaps"][0]
    assert overlap["family"] == "job"
    assert overlap["semantic_scope"] == "deferred_idle_jobs"
    assert overlap["role"] == "state_gateway"
    assert overlap["migration_targets"] == ["vera.idle_queue_service"]
    assert result["family_recommendations"]["job"] == \
        "migrate_duplicate_idle_queue_gateways_to_service_owner"


def test_review_identity_is_order_stable():
    first = build_execution_authority_review(ROOT, CURRENT_SURFACES)
    second = build_execution_authority_review(ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first["review_id"] == second["review_id"]


def test_missing_source_symbol_fails_closed(tmp_path):
    path = tmp_path / "vera" / "sample.py"
    path.parent.mkdir()
    path.write_text("def other():\n    pass\n", encoding="utf-8")
    surface = ExecutionAuthoritySurface(
        "sample", "job", "executor", "sample_scope", "sample.owner",
        "vera/sample.py", ("expected",), True, False)
    with pytest.raises(ValueError, match="source symbols are missing"):
        build_execution_authority_review(tmp_path, (surface,))


def test_observer_cannot_claim_execution():
    with pytest.raises(ValueError, match="cannot execute"):
        ExecutionAuthoritySurface(
            "bad", "job", "observer", "scope", "owner", "vera/bad.py",
            ("watch",), True, False)


def test_source_path_cannot_escape_repo():
    with pytest.raises(ValueError, match="within vera"):
        ExecutionAuthoritySurface(
            "bad", "job", "executor", "scope", "owner", "../outside.py",
            ("run",), True, False)
