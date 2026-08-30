import asyncio
import sys

import pytest

from vera.execution.a2a_adapter import (
    A2AClientPlan,
    A2AServerPlan,
    compile_a2a_adapter_status,
    plan_non_mutating_task,
)


pytestmark = pytest.mark.critical


def _card():
    return {
        "name": "Read-only Research Agent",
        "description": "Returns references without performing mutations.",
        "version": "2026.08",
        "supportedInterfaces": [{
            "url": "https://agent.example/a2a/v1",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }],
        "capabilities": {"streaming": True, "extensions": []},
        "securityRequirements": [{"oauth": ["research.read"]}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": "research.read", "name": "Research",
            "description": "Return public references.", "tags": ["read"],
        }],
    }


def test_non_mutating_client_plan_is_stable_and_does_not_execute():
    before = {name for name in sys.modules if name == "a2a" or name.startswith("a2a.")}
    analysis, plan = plan_non_mutating_task(
        _card(), skill_id="research.read", local_run_id="run_local_01",
        message_id="message_01", auth_reference="secretref:a2a/research")
    assert analysis.accepted
    assert plan == plan_non_mutating_task(
        _card(), skill_id="research.read", local_run_id="run_local_01",
        message_id="message_01", auth_reference="secretref:a2a/research")[1]
    result = plan.to_dict()
    assert result["operation"] == "message/send"
    assert result["canonical_task"] == "a2a.skill/research.read"
    assert result["effects"] == ["none"]
    assert result["transport_status"] == "queued_live"
    assert result["credentials_resolved"] is False
    assert result["request_sent"] is False
    assert result["executes"] is False
    after = {name for name in sys.modules if name == "a2a" or name.startswith("a2a.")}
    assert after == before == set()


def test_client_plan_fails_closed_on_effects_policy_and_task_identity():
    base = dict(
        operation="message/send", endpoint_origin="https://agent.example",
        local_run_id="run_01", canonical_task="a2a.skill/research.read",
        message_id="message_01", auth_reference="secretref:a2a/research",
        effects=("none",), policy_decision="allow_non_mutating")
    with pytest.raises(ValueError, match="effects"):
        A2AClientPlan(**{**base, "effects": ("network",)})
    with pytest.raises(ValueError, match="non-mutating"):
        A2AClientPlan(**{**base, "policy_decision": "allow"})
    with pytest.raises(ValueError, match="server-assigned"):
        A2AClientPlan(**{**base, "operation": "tasks/cancel"})


def test_server_plan_is_an_inert_authenticated_exposure_contract():
    plan = A2AServerPlan(
        service_name="Vera", service_version="2026.08",
        endpoint_origin="https://vera.example",
        exposed_tasks=("research.read",), auth_schemes=("oauth2",))
    result = plan.to_dict()
    assert result["listener_status"] == "queued_live"
    assert result["listener_started"] is False
    assert result["registers_capabilities"] is False
    assert result["executes"] is False
    with pytest.raises(ValueError, match="authentication"):
        A2AServerPlan("Vera", "1", "https://vera.example", ("research.read",), ())


def test_adapter_status_is_honest_about_contracts_and_live_gaps():
    status = compile_a2a_adapter_status()
    assert status["client_plan_contract"] == "implemented"
    assert status["server_plan_contract"] == "implemented"
    assert status["first_non_mutating_task_plan"] == "implemented"
    assert status["client_transport"] == "queued_live"
    assert status["server_listener"] == "queued_live"
    assert status["network_io"] is False
    assert status["executes"] is False


def test_agentbridge_interoperability_capability_and_panel_expose_status():
    from vera.agentbridges import agentbridge_capabilities as caps
    result = asyncio.run(caps.agentbridge_interoperability.__wrapped__())
    assert result["schema"] == "vera.agentbridge-interoperability/v1"
    assert result["a2a"]["client_plan_contract"] == "implemented"
    assert result["a2a"]["client_transport"] == "queued_live"
    assert result["runtime_matrix"]["candidate_count"] >= 10
    assert result["runtime_matrix"]["dimension_count"] == 15
    assert result["source_intake"]["implemented_states"] == [
        "discovered", "inspected", "proposed"]
    assert result["source_intake"]["supported_kinds"] == ["mcp", "openapi"]
    assert result["source_intake"]["network_io"] is False
    assert result["source_intake"]["executes"] is False
    assert result["source_intake"]["build_plan_contract"] == "implemented"
    assert result["source_intake"]["build_source_kinds"] == [
        "cli", "oci", "python", "repository"]
    assert result["source_intake"]["build_execution"] == "queued_live"
    assert result["source_intake"]["activation_execution"] == "queued_live"
    contracts = {item["id"]: item for item in result["shared_contracts"]}
    assert contracts["capability_v2"]["capability"] == "cap.contract.manifest"
    assert contracts["resolver_shadow"]["capability"] == "cap.resolve.shadow"
    assert contracts["source_inspection"]["capability"] == "integration.source.inspect"
    assert contracts["source_build_plan"]["capability"] == "integration.source.build.plan"
    assert result["network_io"] is False
    assert result["executes"] is False
    panel = caps._PANEL_HTML_PATH.read_text(encoding="utf-8")
    assert "/agentbridge/interoperability" in panel
    assert "A2A · LIB02" in panel
    assert "Runtime matrix · LIB18" in panel
    assert "Source intake · W3-06" in panel
    assert "W3-07 build plan" in panel
    assert "Shared Vera contracts" in panel
