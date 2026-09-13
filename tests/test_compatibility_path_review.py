from dataclasses import replace
from pathlib import Path

import pytest

from vera.inventory.compatibility_path_review import (
    CURRENT_PATHS,
    build_compatibility_path_review_set,
    review_compatibility_path,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_each_path_has_independent_payload_free_source_evidence():
    result = build_compatibility_path_review_set(ROOT)
    assert len(result["reviews"]) == len(CURRENT_PATHS) == 10
    assert len({item["candidate_id"] for item in result["reviews"]}) == 10
    assert len({item["review_id"] for item in result["reviews"]}) == 10
    for review in result["reviews"]:
        assert review["independent"] is True
        assert review["removal_authority"] is False
        assert review["executes"] is False
        assert review["imports_reviewed_module"] is False
        assert review["mutates"] is False
        assert "source_text" not in review["source_evidence"]


def test_order_does_not_change_review_set_identity():
    first = build_compatibility_path_review_set(ROOT)
    second = build_compatibility_path_review_set(ROOT, tuple(reversed(CURRENT_PATHS)))
    assert first == second


def test_no_path_is_misclassified_as_confirmed_dead_or_removable():
    result = build_compatibility_path_review_set(ROOT)
    assert result["removal_candidates"] == []
    assert result["conclusion"] == "no_path_has_complete_removal_evidence"
    assert result["bulk_removal_authority"] is False
    assert set(result["decisions"]["insufficient_evidence"]) == {
        "memory.patch_new_cap", "memory.record_cap_interaction"}
    assert result["decisions"]["migrate"] == ["memory.patch_capability_for_memory"]


def test_research_aliases_are_active_callable_compatibility_not_dead_code():
    result = build_compatibility_path_review_set(ROOT)
    retained = set(result["decisions"]["retain"])
    assert retained == {
        "research.report", "research.parallel", "research.deep", "research.code",
        "research.guide", "research.filestore", "research.quick_search"}
    by_name = {item["name"]: item for item in result["reviews"]}
    for name in retained:
        assert by_name[name]["in_repo_runtime_consumer"] is True
        assert by_name[name]["source_evidence"]["runtime_source"].endswith(
            "researcher_api.py")


def test_noop_shims_require_external_inventory_and_internal_call_migrates_first():
    result = build_compatibility_path_review_set(ROOT)
    by_name = {item["name"]: item for item in result["reviews"]}
    startup = by_name["memory.patch_capability_for_memory"]
    assert startup["in_repo_runtime_consumer"] is True
    assert "remove_internal_startup_call_first" in startup["required_actions"]
    for name in ("memory.record_cap_interaction", "memory.patch_new_cap"):
        assert by_name[name]["coverage"]["external_consumers"] == "not_examined"
        assert "retain_import_shim" in by_name[name]["required_actions"]


def test_duplicate_candidates_and_changed_sources_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        build_compatibility_path_review_set(ROOT, (CURRENT_PATHS[0], CURRENT_PATHS[0]))
    with pytest.raises(ValueError, match="source is unavailable"):
        review_compatibility_path(tmp_path, CURRENT_PATHS[0])


def test_invalid_decision_is_rejected():
    with pytest.raises(ValueError, match="decision"):
        replace(CURRENT_PATHS[0], decision="remove")
