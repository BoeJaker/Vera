import asyncio
import sys

import pytest

pytestmark = pytest.mark.critical

from vera.agentbridges.runtime_matrix import (
    DIMENSIONS, FeatureAssessment, RuntimeCandidate, compile_runtime_matrix,
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
