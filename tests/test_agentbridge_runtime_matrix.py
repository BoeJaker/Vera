import asyncio
import sys

import pytest

pytestmark = pytest.mark.critical

from vera.agentbridges.runtime_matrix import (
    DIMENSIONS, FeatureAssessment, RuntimeCandidate, RuntimeLiveObservation,
    compile_runtime_matrix, evaluate_live_evidence,
)


def test_matrix_is_stable_complete_and_non_executing():
    before = {name for name in sys.modules if name.startswith((
        "langgraph", "pydantic_ai", "smolagents", "google.adk", "agents", "agno", "strands"))}
    matrix = compile_runtime_matrix()
    assert matrix == compile_runtime_matrix()
    assert matrix.matrix_id.startswith("runtime_matrix_")
    result = matrix.to_dict()
    assert result["candidate_count"] == 10
    assert result["dimensions"] == list(DIMENSIONS)
    assert result["execution_lane"] == "queued_live"
    assert result["ready_for_selection"] is False
    assert result["universal_winner"] is None
    assert result["imports_runtimes"] is result["executes"] is False
    after = {name for name in sys.modules if name.startswith((
        "langgraph", "pydantic_ai", "smolagents", "google.adk", "agents", "agno", "strands"))}
    assert after == before


def test_every_candidate_assesses_every_dimension_and_queues_execution():
    for candidate in compile_runtime_matrix().to_dict()["candidates"]:
        assert {item["dimension"] for item in candidate["assessments"]} == set(DIMENSIONS)
        assert candidate["execution_lane"] == "queued_live"
        assert candidate["executes"] is False
        assert candidate["gaps"]


def test_shipped_and_prospective_integrations_are_not_conflated():
    candidates = {item.runtime_id: item for item in compile_runtime_matrix().candidates}
    assert {key for key, item in candidates.items() if item.integration == "shipped_bridge"} == {
        "langgraph", "pydanticai", "smolagents"}
    assert candidates["vera-native"].integration == "native"
    assert candidates["hermes-compatible"].integration == "compatibility_path"
    assert candidates["openai-agents"].integration == "prospective"
    openai = {item.dimension: item for item in candidates["openai-agents"].assessments}
    assert openai["tools"].upstream == "supported"
    assert openai["tools"].vera == "not_integrated"


def test_langgraph_lifecycle_and_packages_derive_from_runtime_adapter():
    from vera.agentbridges.runtime_registry import RUNTIME_ADAPTERS

    candidates = {item.runtime_id: item for item in compile_runtime_matrix().candidates}
    langgraph = candidates["langgraph"]
    descriptor = RUNTIME_ADAPTERS["langgraph"].descriptor
    assert langgraph.package_refs == descriptor.package_refs
    assessments = {item.dimension: item for item in langgraph.assessments}
    for dimension in ("streaming", "cancellation", "resources", "teardown", "sandbox"):
        assert assessments[dimension].vera == "supported"
        assert assessments[dimension].evidence.startswith("RuntimeAdapter declaration:")
    assert assessments["recovery"].vera == "not_integrated"
    assert assessments["policy"].vera == "not_integrated"


def test_adapter_projection_rejects_identity_confusion():
    from dataclasses import replace
    from vera.agentbridges.runtime_matrix import _apply_adapter
    from vera.agentbridges.runtime_registry import RUNTIME_ADAPTERS

    candidate = compile_runtime_matrix().candidates[0]
    descriptor = replace(RUNTIME_ADAPTERS["langgraph"].descriptor,
                         runtime_id="different")
    with pytest.raises(ValueError, match="IDs must match"):
        _apply_adapter(candidate, descriptor)


def test_live_gate_covers_roadmap_failure_and_lifecycle_cases():
    cases = set(compile_runtime_matrix().required_live_cases)
    assert cases == {
        "runtime.timeout", "runtime.crash", "runtime.cancel", "runtime.cleanup",
        "runtime.resource_release", "runtime.malicious_output",
        "runtime.missing_dependency", "runtime.version_report",
        "runtime.session_resume", "runtime.trace_redaction",
    }


def test_invalid_or_incomplete_records_fail_closed():
    with pytest.raises(ValueError, match="unknown runtime dimension"):
        FeatureAssessment("magic", "supported", "not_integrated", "claim")
    candidate = compile_runtime_matrix().candidates[0]
    values = candidate.__dict__.copy()
    values["assessments"] = candidate.assessments[:-1]
    with pytest.raises(ValueError, match="assess every runtime dimension"):
        RuntimeCandidate(**values)
    values["assessments"] = candidate.assessments + (candidate.assessments[0],)
    with pytest.raises(ValueError, match="assess every runtime dimension"):
        RuntimeCandidate(**values)


def test_capability_returns_the_same_static_matrix():
    from vera.agentbridges import agentbridge_capabilities as caps
    result = asyncio.run(caps.agentbridge_runtime_matrix.__wrapped__())
    assert result == compile_runtime_matrix().to_dict()
    assert result["executes"] is False


def test_live_evidence_is_payload_free_stable_and_never_selects_a_winner():
    cases = compile_runtime_matrix().required_live_cases
    observations = [RuntimeLiveObservation(
        runtime_id="langgraph", case=case, state="passed",
        reason_code="verified", terminal_count=(0 if case == "runtime.version_report" else 1),
        cleanup_verified=(case == "runtime.cleanup"),
        resource_release_verified=(case == "runtime.resource_release"),
    ) for case in cases]
    first = evaluate_live_evidence(observations, ["langgraph"])
    second = evaluate_live_evidence(reversed(observations), ["langgraph"])
    assert first == second
    assert first["evidence_complete"] is True
    assert first["summaries"][0]["conformant"] is True
    assert first["ready_for_selection"] is False
    assert first["universal_winner"] is None
    assert first["payloads_retained"] is False
    assert first["executes"] is False
    assert "prompt" not in str(first).lower()
    assert "answer" not in str(first).lower()
    assert "stderr" not in str(first).lower()


def test_live_evidence_preserves_missing_failed_and_unavailable_distinctions():
    report = evaluate_live_evidence([
        {"runtime_id": "smolagents", "case": "runtime.timeout",
         "state": "passed", "reason_code": "timeout_bounded", "terminal_count": 1},
        {"runtime_id": "smolagents", "case": "runtime.cancel",
         "state": "unavailable", "reason_code": "adapter_not_registered"},
        {"runtime_id": "pydanticai", "case": "runtime.timeout",
         "state": "failed", "reason_code": "container_leaked", "terminal_count": 1},
    ], ["pydanticai", "smolagents"])
    by_id = {item["runtime_id"]: item for item in report["summaries"]}
    assert by_id["smolagents"]["counts"] == {
        "failed": 0, "passed": 1, "unavailable": 1}
    assert by_id["pydanticai"]["counts"]["failed"] == 1
    assert report["evidence_complete"] is False
    assert all(not item["conformant"] for item in report["summaries"])


def test_live_evidence_rejects_forged_duplicates_and_unproven_passes():
    with pytest.raises(ValueError, match="cleanup pass"):
        RuntimeLiveObservation(
            "langgraph", "runtime.cleanup", "passed", "claimed")
    with pytest.raises(ValueError, match="resource release pass"):
        RuntimeLiveObservation(
            "langgraph", "runtime.resource_release", "passed", "claimed")
    duplicate = {
        "runtime_id": "langgraph", "case": "runtime.timeout",
        "state": "failed", "reason_code": "timeout_missing",
    }
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_live_evidence([duplicate, duplicate], ["langgraph"])
    with pytest.raises(ValueError, match="not selected"):
        evaluate_live_evidence([duplicate], ["smolagents"])
    with pytest.raises(ValueError, match="not declared"):
        evaluate_live_evidence([{
            **duplicate, "case": "runtime.magic"}], ["langgraph"])
    with pytest.raises(ValueError, match="runtime count"):
        evaluate_live_evidence([], [f"runtime-{index}" for index in range(33)])
    with pytest.raises(ValueError, match="observation count"):
        evaluate_live_evidence([duplicate] * 1_025, ["langgraph"])


def test_live_evidence_capability_fails_closed_without_execution():
    from vera.agentbridges import agentbridge_capabilities as caps

    result = asyncio.run(caps.agentbridge_runtime_matrix_evaluate.__wrapped__(
        observations=[{
            "runtime_id": "langgraph", "case": "runtime.version_report",
            "state": "passed", "reason_code": "verified",
        }],
        selected_runtime_ids=["langgraph"],
    ))
    assert result["executes"] is False
    assert result["evidence_complete"] is False
    rejected = asyncio.run(caps.agentbridge_runtime_matrix_evaluate.__wrapped__(
        observations=[], selected_runtime_ids=[]))
    assert rejected["ok"] is False


def test_agent_bridge_exposes_evidence_boundary_without_claiming_selection():
    from pathlib import Path
    from vera.agentbridges import agentbridge_capabilities as caps

    status = asyncio.run(caps.agentbridge_interoperability.__wrapped__())
    runtime = status["runtime_matrix"]
    assert runtime["required_live_cases"] == 10
    assert runtime["evidence_evaluator_registered"] is True
    assert runtime["ready_for_selection"] is False
    shared = {item["id"]: item for item in status["shared_contracts"]}
    assert shared["runtime_matrix_evidence"]["registered"] is True

    panel = (Path(__file__).parents[1] / "vera" / "agentbridges" /
             "agentbridge_catalog_panel.html").read_text(encoding="utf-8")
    assert "conformance cases" in panel
    assert "evidence ${m.evidence_evaluator_registered?'validated':'unavailable'}" in panel
    assert "live cases queued" not in panel
