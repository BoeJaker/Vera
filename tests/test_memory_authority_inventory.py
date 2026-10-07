from dataclasses import replace
from pathlib import Path

import pytest

from vera.fabric.memory_authority_inventory import (
    ROUTES,
    MemoryAuthorityRoute,
    build_memory_authority_inventory,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_inventory_is_source_bound_stable_and_non_mutating():
    first = build_memory_authority_inventory(ROOT)
    second = build_memory_authority_inventory(ROOT, tuple(reversed(ROUTES)))
    assert first == second
    assert first["inventory_id"].startswith("mai_")
    assert first["coverage"]["in_repository_direct_routes"] == "complete_for_bound_sources"
    assert first["coverage"]["stored_record_instances"] == "not_read"
    assert first["coverage"]["live_backends"] == "not_contacted"
    assert first["changes_write_path"] is False
    assert first["changes_read_path"] is False
    assert first["mutates"] is False


def test_inventory_names_direct_writers_consumers_and_stored_identities():
    result = build_memory_authority_inventory(ROOT)
    by_kind = result["routes_by_kind"]
    assert set(by_kind) == {"writer", "mutator", "reader", "stored_reference"}
    assert "memory.cap.store" in by_kind["writer"]
    assert "memory.hook.agent_turn" in by_kind["writer"]
    assert "memory.hook.dag_execution" in by_kind["writer"]
    assert "memory.consumer.agents" in by_kind["reader"]
    assert "memory.consumer.project" in by_kind["stored_reference"]
    identities = result["stored_identities"]
    assert identities["native_record"] == "MemoryRecord.id"
    assert identities["canonical_revision"] == "RecordRevision.revision_id"
    assert identities["stored_project_reference"].endswith("MemoryRecord.id")


def test_inventory_refuses_to_claim_dynamic_or_external_coverage():
    coverage = build_memory_authority_inventory(ROOT)["coverage"]
    assert coverage["dynamic_python_callers"] == "not_observed"
    assert coverage["external_consumers"] == "not_observed"
    required = build_memory_authority_inventory(ROOT)["required_before_authority_switch"]
    assert "runtime_caller_observation" in required
    assert "external_consumer_evidence" in required


def test_route_validation_and_duplicates_fail_closed():
    with pytest.raises(ValueError, match="unique"):
        build_memory_authority_inventory(ROOT, (ROUTES[0], ROUTES[0]))
    with pytest.raises(ValueError, match="kind"):
        replace(ROUTES[0], kind="authority")
    with pytest.raises(ValueError, match="operations"):
        MemoryAuthorityRoute(
            "memory.invalid", "vera/fabric/memory.py", "symbol", "writer", (), "id")


def test_missing_or_changed_source_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="source is unavailable"):
        build_memory_authority_inventory(tmp_path)
