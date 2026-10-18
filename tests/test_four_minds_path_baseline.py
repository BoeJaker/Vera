from dataclasses import replace
from pathlib import Path

import pytest

from vera.inventory.four_minds_path_baseline import (
    GAPS, PATHS, build_four_minds_path_baseline, semantic_baseline,
)


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.critical


def test_inventory_maps_real_decision_path_without_moving_authority():
    result = build_four_minds_path_baseline(ROOT)
    assert result["schema"] == "vera.four-minds-path-baseline/v1"
    assert result["counts"] == {
        "paths": 23, "stages": 9, "gaps": len(GAPS),
        "live": 12, "shadow": 4, "timed": 14,
    }
    assert result["authority_moved"] is False
    assert result["runtime_changed"] is False
    assert result["joined_evidence_complete"] is False
    assert len(result["source_evidence"]) == len(PATHS)
    assert result["constraints"] == {
        "imports_inventoried_subsystems": False,
        "invokes_capabilities": False,
        "contacts_nodes": False,
        "contacts_models": False,
        "uses_accelerators": False,
        "mutates": False,
    }


def test_distributed_compute_and_durable_chain_are_classified_truthfully():
    result = semantic_baseline()
    paths = {value["path_id"]: value for value in result["paths"]}
    assert paths["loop.decision-producer"]["authority"] == "agent-loop"
    assert paths["decision.ledger-stream"]["target"] == "decision.ledger-postgres"
    assert paths["decision.ledger-postgres"]["authority"] == "postgres"
    assert paths["distributed-decide.dispatch"]["authority"] == "nlp-node"
    assert paths["distributed-decide.knn"]["identities"] == [
        "index-id", "index-version", "row-id", "run-id"]
    assert paths["precedent.lookup"]["runtime_state"] == "default_off"
    assert paths["decision.student-router"]["runtime_state"] == "default_off"


def test_models_context_and_worldview_gaps_are_not_hidden_by_labels():
    result = semantic_baseline()
    paths = {value["path_id"]: value for value in result["paths"]}
    assert paths["decision.shadow-heads"]["contract"] == "decision.shadow-onnx"
    assert paths["sense.nlp-inputs"]["identities"] == [
        "text-hash", "cards-version"]
    assert paths["worldview.spread-monitor"]["stage"] == "monitoring"
    assert "context_evidence_is_not_an_exact_provenance_qualified_decision_input" in result["gaps"]
    assert "jepa_worldview_is_monitored_but_not_joined_as_a_decision_input" in result["gaps"]
    assert "distributed_decide_retains_node_and_index_but_not_portable_modelpackage_identity" in result["gaps"]


def test_baseline_identity_is_order_stable_and_source_bound():
    first = semantic_baseline()
    second = semantic_baseline(tuple(reversed(PATHS)))
    assert first == second
    inventory = build_four_minds_path_baseline(ROOT)
    assert inventory["baseline_id"] == first["baseline_id"]
    assert inventory["inventory_id"].startswith("fmpi_")


def test_duplicate_paths_and_missing_source_assertions_fail_closed():
    with pytest.raises(ValueError, match="uniquely identified"):
        semantic_baseline(PATHS + (PATHS[0],))
    missing = replace(PATHS[0], source_assertion="not present in source")
    with pytest.raises(ValueError, match="source assertion missing"):
        build_four_minds_path_baseline(ROOT, (missing,) + PATHS[1:])


def test_inventory_module_has_no_operational_dependencies():
    source = (ROOT / "vera" / "inventory" /
              "four_minds_path_baseline.py").read_text(encoding="utf-8")
    for forbidden in (
            "capability_orchestration import", "minds import", "nlp_dispatch import",
            "model_binding_core import", "requests", "httpx", "torch",
            "tensorflow", "numpy", "subprocess"):
        assert forbidden not in source
