from pathlib import Path

import pytest

from vera.inventory.bridge_runtime_boilerplate_review import (
    BridgeLifecycleSurface, CURRENT_SURFACES,
    build_bridge_runtime_boilerplate_review)

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_review_is_source_bound_and_performs_no_lifecycle_action():
    result = build_bridge_runtime_boilerplate_review(ROOT)
    assert result["schema"] == "vera.bridge-runtime-boilerplate-review/v1"
    assert len(result["surfaces"]) == len(CURRENT_SURFACES) == 3
    for key in ("removal_authority", "changes_registration", "changes_execution",
                "inspects_docker", "builds_images", "launches_bridges",
                "contacts_models", "mutates"):
        assert result[key] is False
    assert all(row["source_content_digest"].startswith("sha256:")
               for row in result["source_evidence"].values())


def test_review_distinguishes_full_adapter_from_shared_runner_only():
    result = build_bridge_runtime_boilerplate_review(ROOT)
    assert result["runtimes_by_integration_level"] == {
        "adapter_facade": ["langgraph"],
        "shared_runner_only": ["pydanticai", "smolagents"]}
    assert result["shared_authority"]["lifecycle_contract"] == "RuntimeAdapter"
    assert result["shared_authority"]["container_execution"] == "stream_bridge_container"


def test_recommendation_retains_provider_semantics_and_public_identities():
    result = build_bridge_runtime_boilerplate_review(ROOT)
    assert "retain_per_bridge_capability_names_and_event_prefixes" in result["recommendations"]
    assert "retain_provider_specific_argv_progress_and_error_semantics" in result["recommendations"]
    assert result["coverage"]["live_bridge_runs"] == "not_run"
    assert result["coverage"]["external_consumers"] == "not_examined"
    assert not any(item.startswith(("remove_", "disable_", "retire_"))
                   for item in result["recommendations"])


def test_identity_is_order_stable():
    first = build_bridge_runtime_boilerplate_review(ROOT, CURRENT_SURFACES)
    second = build_bridge_runtime_boilerplate_review(
        ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first["review_id"] == second["review_id"]


def test_source_drift_fails_closed(tmp_path):
    source = tmp_path / "vera" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("present = True\n", encoding="utf-8")
    surface = BridgeLifecycleSurface(
        "sample", "vera/sample.py", "vera/sample.py", "vera/sample.py",
        "shared_runner_only", False)
    with pytest.raises(ValueError, match="source assertions are missing"):
        build_bridge_runtime_boilerplate_review(
            tmp_path, (surface,), {"vera/sample.py": ("missing = True",)})


def test_surface_validation_fails_closed():
    with pytest.raises(ValueError, match="unsupported bridge integration level"):
        BridgeLifecycleSurface(
            "sample", "vera/a.py", "vera/a.py", "vera/a.py", "guessed", False)
    with pytest.raises(ValueError, match="stay within vera"):
        BridgeLifecycleSurface(
            "sample", "../a.py", "vera/a.py", "vera/a.py",
            "shared_runner_only", False)
