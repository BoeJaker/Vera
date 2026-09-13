from pathlib import Path

import pytest

from vera.inventory.agent_loop_authority_review import (
    CURRENT_SURFACES,
    AgentLoopSurface,
    build_agent_loop_authority_review,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_current_loop_manifest_is_source_bound_and_non_executing():
    result = build_agent_loop_authority_review(ROOT)
    assert result["schema"] == "vera.agent-loop-authority-review/v1"
    assert len(result["surfaces"]) == len(CURRENT_SURFACES) == 12
    assert result["removal_authority"] is False
    assert result["executes"] is False and result["mutates"] is False
    assert all(row["source_content_digest"].startswith("sha256:")
               for row in result["source_evidence"])


def test_five_independent_generic_executors_offer_a_shared_kernel_seam():
    result = build_agent_loop_authority_review(ROOT)
    ids = result["parallel_generic_executor_surface_ids"]
    names = {row["name"] for row in result["surfaces"]
             if row["surface_id"] in ids}
    assert names == {"agent_loop.v1", "agent_loop.v2", "agent_loop.v3",
                     "agent_loop.v4", "agent_loop.v5"}
    assert "share_step_kernel_without_collapsing_numbered_loop_modes" in \
        result["recommendations"]


def test_v6_v7_v8_and_domain_loop_are_not_mislabeled_as_duplicate_executors():
    result = build_agent_loop_authority_review(ROOT)
    by_name = {row["name"]: row for row in result["surfaces"]}
    assert by_name["agent_loop.v6"]["role"] == "controller"
    assert by_name["agent_loop.v7"]["delegates_to"] == "dag.agent_loop_v6"
    assert by_name["agent_loop.v8"]["role"] == "program_orchestrator"
    assert by_name["operator.browser_loop"]["semantic_scope"] == \
        "browser_observe_think_act"


def test_all_numbered_loop_modes_are_retained_and_have_consumer_evidence():
    result = build_agent_loop_authority_review(ROOT)
    by_id = {row["surface_id"]: row for row in result["surfaces"]}
    numbered_modes = [row for row in result["surfaces"]
                      if row["name"].startswith("agent_loop.v")]
    assert {row["name"] for row in numbered_modes} == {
        f"agent_loop.v{number}" for number in range(1, 9)
    }
    assert result["v_label_semantics"] == "named_loop_mode_not_version_order"
    assert result["retain_all_numbered_modes"] is True
    assert "retain_all_numbered_loop_modes_and_public_identities" in \
        result["recommendations"]
    forbidden = ("remove", "retire", "deprecat", "migrate")
    assert not any(token in recommendation
                   for recommendation in result["recommendations"]
                   for token in forbidden)
    for surface in numbered_modes:
        assert result["consumer_reference_counts"][surface["surface_id"]] > 0
    assert all("source_path" not in row for row in result["consumer_evidence"])
    assert all(by_id[row["surface_id"]]["capability"] != "none"
               for row in result["consumer_evidence"])


def test_review_identity_is_order_stable():
    first = build_agent_loop_authority_review(ROOT, CURRENT_SURFACES)
    second = build_agent_loop_authority_review(ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first["review_id"] == second["review_id"]


def test_missing_symbol_or_capability_declaration_fails_closed(tmp_path):
    source = tmp_path / "vera" / "sample.py"
    source.parent.mkdir()
    source.write_text("async def execute():\n    return None\n", encoding="utf-8")
    missing_symbol = AgentLoopSurface(
        "sample", "executor", "generic", "vera/sample.py", "absent",
        "sample.loop", strategy="sample")
    with pytest.raises(ValueError, match="source symbol is missing"):
        build_agent_loop_authority_review(tmp_path, (missing_symbol,))
    missing_cap = AgentLoopSurface(
        "sample", "executor", "generic", "vera/sample.py", "execute",
        "sample.loop", strategy="sample")
    with pytest.raises(ValueError, match="capability declaration is missing"):
        build_agent_loop_authority_review(tmp_path, (missing_cap,))


def test_delegating_roles_require_a_target_and_paths_cannot_escape():
    with pytest.raises(ValueError, match="require a target"):
        AgentLoopSurface("bad", "adapter", "generic", "vera/bad.py", "run")
    with pytest.raises(ValueError, match="within vera"):
        AgentLoopSurface("bad", "executor", "generic", "../bad.py", "run")
