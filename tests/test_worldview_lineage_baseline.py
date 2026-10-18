from dataclasses import replace
from pathlib import Path

import pytest

from vera.inventory.worldview_lineage_baseline import (
    GAPS, LINEAGES, SURFACES, build_worldview_lineage_baseline,
    semantic_baseline,
)


ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.critical


def test_inventory_proves_three_distinct_lineages_and_current_surfaces():
    result = build_worldview_lineage_baseline(ROOT)
    assert result["schema"] == "vera.worldview-lineage-baseline/v1"
    assert result["counts"] == {
        "lineages": 3, "surfaces": 19, "live_or_default": 8,
        "gaps": len(GAPS),
    }
    assert {value["lineage_id"] for value in result["lineages"]} == {
        "worldview.jepa", "worldview.non-jepa", "godseye.integrated"}
    by_id = {value["lineage_id"]: value for value in result["lineages"]}
    assert by_id["worldview.non-jepa"]["runtime_state"] == "unserved"
    assert by_id["godseye.integrated"]["successor_of"] == "worldview.non-jepa"
    assert by_id["worldview.jepa"]["successor_of"] == ""
    assert result["authority_shared"] is False
    assert result["runtime_changed"] is False
    assert result["constraints"] == {
        "imports_inventoried_subsystems": False,
        "invokes_capabilities": False,
        "contacts_sources": False,
        "contacts_models": False,
        "uses_accelerators": False,
        "mutates": False,
    }
    assert len(result["source_evidence"]) == len(LINEAGES) + len(SURFACES)


def test_consumers_exports_and_gaps_are_explicit_without_identity_conflation():
    result = semantic_baseline()
    surfaces = {value["surface_id"]: value for value in result["surfaces"]}
    assert surfaces["jepa.context-recall"]["runtime_state"] == "opt_in"
    assert surfaces["jepa.research"]["runtime_state"] == "default_on"
    assert surfaces["non-jepa.page"]["runtime_state"] == "unserved"
    assert surfaces["non-jepa.portable-dataset"]["runtime_state"] == "offline"
    assert surfaces["godseye.portable-dataset"]["runtime_state"] == "offline"
    assert surfaces["jepa.evidence-envelope"]["runtime_state"] == "test_only"
    assert surfaces["godseye.cctv"]["contract"] == "godseye.cctv-native"
    assert "jepa_does_not_consume_non_jepa_or_godseye_snapshots" in result["gaps"]
    assert len({value["authority"] for value in result["lineages"]}) == 3


def test_baseline_identity_is_order_stable_and_source_bound():
    first = semantic_baseline()
    second = semantic_baseline(tuple(reversed(LINEAGES)), tuple(reversed(SURFACES)))
    assert first == second
    inventory = build_worldview_lineage_baseline(ROOT)
    assert inventory["baseline_id"] == first["baseline_id"]
    assert inventory["inventory_id"].startswith("wvli_")


def test_unknown_lineages_duplicates_and_missing_assertions_fail_closed():
    with pytest.raises(ValueError, match="exactly three unique"):
        semantic_baseline(LINEAGES + (LINEAGES[0],), SURFACES)
    forged = replace(SURFACES[0], lineage_id="worldview.unknown")
    with pytest.raises(ValueError, match="unknown lineage"):
        semantic_baseline(LINEAGES, (forged,) + SURFACES[1:])
    missing = replace(SURFACES[0], source_assertion="not present in source")
    with pytest.raises(ValueError, match="source assertion missing"):
        build_worldview_lineage_baseline(ROOT, LINEAGES, (missing,) + SURFACES[1:])


def test_inventory_module_has_no_runtime_or_model_dependency():
    source = (ROOT / "vera" / "inventory" /
              "worldview_lineage_baseline.py").read_text(encoding="utf-8")
    for forbidden in ("capability_orchestration import", "worldview_jepa import",
                      "godseye_capabilities import", "requests", "httpx",
                      "torch", "tensorflow", "subprocess"):
        assert forbidden not in source
