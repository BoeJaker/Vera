from pathlib import Path

import pytest

from vera.inventory.context_capability_probe_review import (
    ContextCapabilityProbe, CURRENT_PROBES,
    build_context_capability_probe_review)

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_review_is_source_bound_non_executing_and_non_mutating():
    result = build_context_capability_probe_review(ROOT)
    assert result["schema"] == "vera.context-capability-probe-review/v1"
    assert len(result["probes"]) == len(CURRENT_PROBES) == 6
    for key in ("removal_authority", "changes_resolution",
                "changes_bootstrap_policy", "invokes_capabilities",
                "imports_optional_subsystems", "contacts_models", "mutates"):
        assert result[key] is False
    assert result["source_evidence"]["source_content_digest"].startswith("sha256:")


def test_related_qa_fallback_order_is_explicit_and_preserved():
    result = build_context_capability_probe_review(ROOT)
    probes = {item["capability"]: item for item in result["probes"]}
    fallback = probes["memory.recall_2nd_order"]
    assert fallback["probe_kind"] == "compatibility_fallback"
    assert fallback["ordered_after"] == "context.related_qa_block"
    assert result["capabilities_by_semantic_role"]["context.related_qa"] == [
        "context.related_qa_block", "memory.recall_2nd_order"]


def test_worldview_scope_is_explicitly_jepa_and_does_not_alias_other_lineages():
    result = build_context_capability_probe_review(ROOT)
    scope = result["worldview_scope"]
    assert scope["target"] == "jepa_worldview"
    assert scope["capabilities"] == ["worldview.query", "worldview.rollout"]
    assert scope["not_targeted"] == ["non_jepa_worldview", "godseye"]
    assert "do_not_alias_non_jepa_worldview_or_godseye_to_jepa_capabilities" in (
        result["recommendations"])


def test_bootstrap_lists_are_not_misclassified_as_dependency_resolution():
    result = build_context_capability_probe_review(ROOT)
    assert result["bootstrap_policy"]["classification"] == (
        "static_loop_bootstrap_policy_not_dependency_resolution")
    assert "keep_loop_bootstrap_policy_separate_from_dependency_resolution" in (
        result["recommendations"])


def test_identity_is_order_stable():
    first = build_context_capability_probe_review(ROOT, CURRENT_PROBES)
    second = build_context_capability_probe_review(
        ROOT, tuple(reversed(CURRENT_PROBES)))
    assert first["review_id"] == second["review_id"]


def test_fallback_and_identifiers_fail_closed():
    with pytest.raises(ValueError, match="preferred capability"):
        ContextCapabilityProbe(
            "memory.old", "context.related", "compatibility_fallback", False)
    with pytest.raises(ValueError, match="bounded dotted identifier"):
        ContextCapabilityProbe(
            "bad capability", "context.related", "preferred", False)
    orphan = ContextCapabilityProbe(
        "memory.old", "context.related", "compatibility_fallback", False,
        ordered_after="memory.new")
    with pytest.raises(ValueError, match="declared probe"):
        build_context_capability_probe_review(ROOT, (orphan,))
