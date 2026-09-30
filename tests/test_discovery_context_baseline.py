import json
from pathlib import Path

import pytest

from vera.inventory.discovery_context_baseline import (
    CURRENT_COMPONENTS,
    DiscoveryContextComponent,
    build_discovery_context_baseline,
    semantic_baseline,
)


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden" / "discovery_context_baseline.json"


@pytest.mark.critical
def test_current_inventory_is_source_bound_without_live_work():
    value = build_discovery_context_baseline(ROOT)
    assert value["schema"] == "vera.discovery-context-baseline/v1"
    assert value["counts"] == {
        "components": 28, "live_evidence_required": 6, "gaps": 2,
    }
    assert len(value["source_evidence"]) == value["counts"]["components"]
    assert all(not enabled for enabled in value["constraints"].values())
    assert set(value["components_by_role"]) == {
        "context", "data", "discover", "enrich", "retrieve", "route",
    }
    assert "worldview.jepa-model" in value["components_by_role"]["enrich"]
    assert "godseye.geospatial" in value["components_by_role"]["data"]
    assert "godseye.portable-dataset" in value["components_by_role"]["data"]
    assert "agent.rag-context" in value["components_by_role"]["context"]
    assert "discovery.operator-readmodel" in value["components_by_role"]["data"]


@pytest.mark.critical
def test_semantic_baseline_matches_reviewed_golden():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert semantic_baseline() == expected


def test_inventory_rejects_duplicate_and_dangling_components():
    with pytest.raises(ValueError, match="uniquely identified"):
        semantic_baseline((CURRENT_COMPONENTS[0], CURRENT_COMPONENTS[0]))
    invalid = DiscoveryContextComponent(
        "test.dangling", "context", "vera/context_provider.py",
        "class ContextProvider(Protocol)", "context_provider",
        "provider_injected", "cpu", "vera.test.v1", ("missing.component",))
    with pytest.raises(ValueError, match="unknown components"):
        semantic_baseline((invalid,))


def test_inventory_rejects_unsafe_or_unbounded_declarations(tmp_path):
    with pytest.raises(ValueError, match="repository-relative"):
        DiscoveryContextComponent(
            "test.component", "context", "../outside.py", "class X",
            "context_provider", "inline", "cpu", "vera.test.v1")
    component = DiscoveryContextComponent(
        "test.component", "context", "vera/missing.py", "class X",
        "context_provider", "inline", "cpu", "vera.test.v1")
    with pytest.raises(ValueError, match="source is unavailable"):
        build_discovery_context_baseline(tmp_path, (component,))


def test_source_drift_fails_closed(tmp_path):
    source = tmp_path / "vera" / "context_provider.py"
    source.parent.mkdir(parents=True)
    source.write_text("class SomethingElse:\n    pass\n", encoding="utf-8")
    component = next(value for value in CURRENT_COMPONENTS
                     if value.component_id == "context.portable")
    with pytest.raises(ValueError, match="source assertion missing"):
        build_discovery_context_baseline(tmp_path, (component,))
