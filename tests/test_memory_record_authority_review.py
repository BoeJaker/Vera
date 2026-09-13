from dataclasses import replace
from pathlib import Path

import pytest

from vera.inventory.memory_record_authority_review import (
    CURRENT_SURFACES,
    MemoryAuthoritySurface,
    build_memory_record_authority_review,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_review_is_source_bound_stable_and_non_mutating():
    first = build_memory_record_authority_review(ROOT)
    second = build_memory_record_authority_review(ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first == second
    assert first["review_id"].startswith("mrar_")
    assert len(first["source_evidence"]) == 5
    assert first["removal_authority"] is False
    assert first["changes_write_path"] is False
    assert first["changes_read_path"] is False
    assert first["reads_stored_memory"] is False
    assert first["contacts_backends"] is False
    assert first["contacts_models"] is False
    assert first["mutates"] is False


def test_review_separates_current_native_claim_from_target_authority():
    result = build_memory_record_authority_review(ROOT)
    current = result["current_contract"]
    target = result["target_contract"]
    assert current["native_content_authority_claim"] == "memory.postgres"
    assert current["derived_indexes"] == ["memory.chroma", "memory.neo4j"]
    assert current["atomic_fanout"] is False and current["drift_possible"] is True
    assert target["content_authority"] == "fabric.record_revision"
    assert target["retrieval_contract"] == "memory.provider_projection"
    assert target["legacy_ingress"] == "memory.record"


def test_projections_and_ephemeral_cursors_are_not_content_authorities():
    result = build_memory_record_authority_review(ROOT)
    by_name = {item["surface"]: item for item in result["surfaces"]}
    assert by_name["memory.chroma"]["authority_state"] == "derived_projection"
    assert by_name["memory.neo4j"]["authority_state"] == "derived_projection"
    assert by_name["memory.provider_projection"]["authority_state"] == "derived_projection"
    assert by_name["memory.session_cursors"]["authority_state"] == "ephemeral"
    assert by_name["memory.session_cursors"]["stores_content"] is False


def test_cutover_requires_receipts_reconciliation_and_behavioral_parity():
    result = build_memory_record_authority_review(ROOT)
    recommendations = set(result["recommendations"])
    assert "require_dual_write_receipts_and_reconciliation_before_cutover" in recommendations
    assert "prove_success_error_timeout_cancel_restart_and_recovery_parity" in recommendations
    assert "measure_all_direct_writers_stored_ids_and_external_consumers" in recommendations
    assert result["coverage"]["stored_records"] == "not_examined"
    assert result["coverage"]["live_backends"] == "not_run"


def test_duplicate_surface_is_rejected():
    with pytest.raises(ValueError, match="unique"):
        build_memory_record_authority_review(ROOT, (CURRENT_SURFACES[0], CURRENT_SURFACES[0]))


def test_unknown_role_and_empty_identity_are_rejected():
    with pytest.raises(ValueError, match="role"):
        replace(CURRENT_SURFACES[0], role="database")
    with pytest.raises(ValueError, match="identity"):
        MemoryAuthoritySurface(
            "memory.example", "vera/fabric/memory.py", "native_record",
            True, True, "compatibility", "")


def test_missing_or_changed_source_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="source is unavailable"):
        build_memory_record_authority_review(tmp_path)
