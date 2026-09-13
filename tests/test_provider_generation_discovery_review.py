from pathlib import Path

import pytest

from vera.inventory.provider_generation_discovery_review import (
    CURRENT_SURFACES,
    GenerationSurface,
    build_provider_generation_discovery_review,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_review_is_source_bound_non_executing_and_non_mutating():
    result = build_provider_generation_discovery_review(ROOT)
    assert result["schema"] == "vera.provider-generation-discovery-review/v1"
    assert len(result["surfaces"]) == len(CURRENT_SURFACES) == 7
    assert result["removal_authority"] is False
    assert result["changes_discovery"] is False
    assert result["executes"] is False and result["mutates"] is False
    assert all(row["source_content_digest"].startswith("sha256:")
               for row in result["source_evidence"])


def test_task_providers_are_separate_from_raw_and_direct_provider_entry_points():
    result = build_provider_generation_discovery_review(ROOT)
    classes = result["surfaces_by_discovery_class"]
    assert classes["task_facing_default"] == ["code.author", "prose.author"]
    assert classes["raw_family_excluded"] == ["llm.generate"]
    assert classes["direct_provider_unclassified"] == [
        "ollama.generate_raw", "providers.chat", "vllm.chat", "vllm.generate"]


def test_review_preserves_every_provider_and_grants_no_policy_change():
    result = build_provider_generation_discovery_review(ROOT)
    assert "retain_all_provider_entry_points_as_explicitly_callable" in result["recommendations"]
    assert "add_one_shared_discovery_policy_for_direct_provider_entry_points" in result["recommendations"]
    forbidden = ("remove", "retire", "deprecat", "disable_provider")
    assert not any(token in recommendation
                   for recommendation in result["recommendations"]
                   for token in forbidden)
    assert result["coverage"]["runtime_calls"] == "not_examined"


def test_loaded_vllm_availability_is_not_confused_with_default_discovery():
    result = build_provider_generation_discovery_review(ROOT)
    by_name = {row["name"]: row for row in result["surfaces"]}
    assert by_name["vllm.generate"]["availability_scope"] == "core"
    assert by_name["providers.chat"]["availability_scope"] == "configured_external"
    assert by_name["vllm.generate"]["discovery_class"] == "direct_provider_unclassified"
    assert result["missing_task_contracts"] == [
        "providers.chat", "vllm.chat", "vllm.generate"]
    assert by_name["ollama.generate_raw"]["contract_status"] == "declared"


def test_identity_is_order_stable():
    first = build_provider_generation_discovery_review(ROOT, CURRENT_SURFACES)
    second = build_provider_generation_discovery_review(ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first["review_id"] == second["review_id"]


def test_missing_capability_or_symbol_fails_closed(tmp_path):
    source = tmp_path / "vera" / "sample.py"
    source.parent.mkdir()
    source.write_text('CAP = "sample.generate"\ndef other():\n    pass\n', encoding="utf-8")
    surface = GenerationSurface(
        "sample.generate", "backend_direct", "direct_provider_unclassified",
        "text.generate", "missing", "core", "vera/sample.py", "expected")
    with pytest.raises(ValueError, match="source symbol is missing"):
        build_provider_generation_discovery_review(tmp_path, (surface,))


def test_paths_and_discovery_classes_fail_closed():
    with pytest.raises(ValueError, match="within vera"):
        GenerationSurface("bad.generate", "backend_direct", "direct_provider_unclassified",
                          "text.generate", "missing", "core", "../bad.py", "run")
    with pytest.raises(ValueError, match="unsupported discovery class"):
        GenerationSurface("bad.generate", "backend_direct", "guessed",
                          "text.generate", "missing", "core", "vera/bad.py", "run")
