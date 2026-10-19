from dataclasses import replace
from pathlib import Path

import pytest

from vera.inventory.execution_producer_path_baseline import (
    GAPS, PATHS, build_execution_producer_path_baseline, semantic_baseline,
)


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.critical


def test_inventory_is_source_bound_and_does_not_move_execution_authority():
    result = build_execution_producer_path_baseline(ROOT)
    assert result["schema"] == "vera.execution-producer-path-baseline/v1"
    assert result["counts"] == {
        "paths": 19, "families": 5, "roles": 6,
        "native_executors": 8, "projections": 4,
        "definition_only": 1, "gaps": len(GAPS),
    }
    assert result["authority_moved"] is False
    assert result["runtime_changed"] is False
    assert result["coverage_complete"] is False
    assert len(result["source_evidence"]) == len(PATHS)
    assert not any(result["constraints"].values())


def test_native_execution_and_projection_are_not_conflated():
    paths = {row["path_id"]: row for row in semantic_baseline()["paths"]}
    assert paths["agent-loop.http-stream"]["role"] == "native-executor"
    assert paths["agent-loop.run-projection"]["role"] == "projection"
    assert paths["dream.cycle"]["authority"] == "dream"
    assert paths["dream.run-projection"]["authority"] == "run-protocol"
    assert paths["schedule.calendar-action"]["role"] == "native-executor"
    assert paths["schedule.run-projection"]["role"] == "projection"
    assert paths["schedule.workflow-trigger-projection"]["role"] == "projection"
    assert paths["dag.workflow-definition"]["role"] == "definition"


def test_every_path_reports_all_five_required_coverage_dimensions():
    for row in semantic_baseline()["paths"]:
        assert set((row["progress"], row["cancellation"], row["artifacts"],
                    row["effects"], row["recovery"])) <= {
            "native", "projected", "partial", "absent", "not_applicable"}
    paths = {row["path_id"]: row for row in semantic_baseline()["paths"]}
    assert paths["dag.stored-run"]["cancellation"] == "absent"
    assert paths["dream.cycle"]["artifacts"] == "native"
    assert paths["schedule.calendar-action"]["effects"] == "native"
    assert paths["schedule.user-notification"]["effects"] == "native"
    assert paths["schedule.user-notification"]["cancellation"] == "absent"


def test_flow_builder_is_truthfully_definition_only():
    result = semantic_baseline()
    row = next(row for row in result["paths"]
               if row["path_id"] == "flow-builder.definition")
    assert row["runtime_state"] == "definition_only"
    assert row["role"] == "definition"
    assert row["target"] == "dag.workflow-definition"
    assert "flow_builder_defines_workflows_but_has_no_execution_entrypoint" in result["gaps"]


def test_identity_is_order_stable_and_missing_source_evidence_fails_closed():
    assert semantic_baseline() == semantic_baseline(tuple(reversed(PATHS)))
    inventory = build_execution_producer_path_baseline(ROOT)
    assert inventory["baseline_id"].startswith("eppb_")
    assert inventory["inventory_id"].startswith("eppi_")
    missing = replace(PATHS[0], source_assertion="not present in source")
    with pytest.raises(ValueError, match="source assertion missing"):
        build_execution_producer_path_baseline(ROOT, (missing,) + PATHS[1:])
    with pytest.raises(ValueError, match="uniquely identified"):
        semantic_baseline(PATHS + (PATHS[0],))


def test_inventory_module_has_no_operational_dependencies():
    source = (ROOT / "vera" / "inventory" /
              "execution_producer_path_baseline.py").read_text(encoding="utf-8")
    for forbidden in (
            "capability_orchestration import", "dag_store import",
            "dream_capabilities import", "longterm_scheduler import",
            "run_protocol import", "requests", "httpx", "subprocess",
            "torch", "tensorflow"):
        assert forbidden not in source
