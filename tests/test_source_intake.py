import asyncio
import sys

import pytest

from vera.integrations.source_intake import (
    LIFECYCLE,
    inspect_mcp_source,
    inspect_openapi_source,
    inspect_source,
    lifecycle_contract,
    plan_transition,
)


pytestmark = pytest.mark.critical


def _mcp(**changes):
    value = {
        "id": "research-mcp",
        "label": "Research MCP",
        "protocol_version": "2025-06-18",
        "transport": "streamable_http",
        "url": "https://mcp.example/v1",
        "headers": {"Authorization": "secretref:mcp/research"},
        "tools": [{
            "name": "search.read",
            "description": "Search public material without mutations.",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string"}}},
        }],
    }
    value.update(changes)
    return value


def _openapi(**changes):
    value = {
        "openapi": "3.1.0",
        "info": {"title": "Research API", "version": "1.2.0"},
        "servers": [{"url": "https://api.example/v1"}],
        "paths": {
            "/search": {"get": {
                "operationId": "search.read", "summary": "Search public records",
                "parameters": [{"name": "query", "in": "query",
                                "schema": {"type": "string"}}],
            }},
            "/health": {"get": {"operationId": "health.read"}},
        },
    }
    value.update(changes)
    return value


def test_mcp_fixture_projects_stable_unauthorised_candidates_without_io():
    before = set(sys.modules)
    first = inspect_mcp_source(_mcp())
    assert first == inspect_source("mcp", _mcp())
    result = first.to_dict()
    assert result["kind"] == "mcp"
    assert result["accepted"] is True
    assert result["lifecycle_state"] == "proposed"
    assert result["next_lane"] == "queued_w3_07"
    assert result["metadata"] == {"candidate_count": 1, "transport": "streamable_http"}
    assert result["endpoint_origins"] == ["https://mcp.example"]
    assert result["candidates"] == [{
        "candidate_id": "research-mcp.search.read",
        "canonical_task": "external.research-mcp/search.read",
        "label": "Search public material without mutations.",
        "input_schema_status": "declared", "effects_status": "unknown",
        "authorized": False, "executable": False,
    }]
    assert all(result[key] is False for key in (
        "registers", "installs", "builds", "activates", "uses_secrets",
        "network_io", "executes"))
    assert set(sys.modules) == before


def test_stdio_mcp_is_inspected_as_metadata_and_transport_conflicts_are_issues():
    descriptor = _mcp(transport="stdio", command="uvx", args=["mcp-server-time"])
    descriptor.pop("url")
    assert inspect_mcp_source(descriptor).accepted
    conflicted = {**descriptor, "url": "https://mcp.example"}
    result = inspect_mcp_source(conflicted).to_dict()
    assert not result["accepted"]
    assert result["issues"] == ["stdio_descriptor_must_not_declare_url"]
    proxy = inspect_mcp_source(_mcp(transport="vera_proxy"))
    assert proxy.accepted and dict(proxy.metadata)["transport"] == "vera_proxy"


def test_mcp_plaintext_secrets_unsafe_urls_and_empty_tools_fail_closed():
    with pytest.raises(ValueError, match="plaintext credentials"):
        inspect_mcp_source(_mcp(headers={"Authorization": "Bearer plaintext"}))
    with pytest.raises(ValueError, match="plaintext credentials"):
        inspect_mcp_source(_mcp(env={"GITHUB_TOKEN": "plaintext"}))
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        inspect_mcp_source(_mcp(url="http://mcp.example/v1"))
    result = inspect_mcp_source(_mcp(tools=[])).to_dict()
    assert result["lifecycle_state"] == "inspected"
    assert result["issues"] == ["no_tools_declared"]


def test_openapi_fixture_projects_operations_but_never_authorises_them():
    first = inspect_openapi_source(_openapi(), source_id="research-api")
    assert first == inspect_source("openapi", _openapi(), source_id="research-api")
    result = first.to_dict()
    assert result["accepted"] is True
    assert result["metadata"] == {
        "candidate_count": 2, "path_count": 2,
        "specification_version": "3.1.0"}
    assert result["endpoint_origins"] == ["https://api.example"]
    assert [item["candidate_id"] for item in result["candidates"]] == [
        "research-api.health.read", "research-api.search.read"]
    assert {item["effects_status"] for item in result["candidates"]} == {"unknown"}
    assert not any(item["authorized"] or item["executable"]
                   for item in result["candidates"])
    numeric = _openapi(paths={"/2026": {"get": {
        "operationId": "2026.report", "summary": "Annual report"}}})
    numeric_result = inspect_openapi_source(numeric, source_id="research-api")
    assert numeric_result.candidates[0].candidate_id == "research-api.op-2026.report"


def test_openapi_external_refs_templates_and_malformed_shapes_are_not_followed():
    spec = _openapi(
        servers=[{"url": "https://{tenant}.example/v1"}],
        components={"schemas": {"Remote": {"$ref": "https://schemas.example/x.json"}}})
    result = inspect_openapi_source(spec, source_id="research-api").to_dict()
    assert result["accepted"] is False
    assert result["endpoint_origins"] == []
    assert result["issues"] == [
        "external_refs_require_separate_intake", "no_concrete_https_server",
        "templated_server_requires_review"]
    with pytest.raises(ValueError, match="unsupported OpenAPI"):
        inspect_openapi_source({**_openapi(), "openapi": "4.0.0"})
    with pytest.raises(ValueError, match="non-empty"):
        inspect_openapi_source({**_openapi(), "paths": {}})


def test_lifecycle_is_complete_ordered_and_only_inspection_lane_is_allowed():
    contract = lifecycle_contract()
    assert tuple(contract["states"]) == LIFECYCLE
    assert contract["implemented_states"] == ["discovered", "inspected", "proposed"]
    assert contract["queued_states"] == list(LIFECYCLE[3:])
    inspected = plan_transition("research-api", "discovered", "inspected")
    assert inspected["allowed"] is True and inspected["applied"] is False
    proposed = plan_transition(
        "research-api", "inspected", "proposed", ("srcinspect_abc",))
    assert proposed["allowed"] is True and proposed["lane"] == "deterministic"
    built = plan_transition("research-api", "proposed", "built", ("srcinspect_abc",))
    assert built["allowed"] is False
    assert built["lane"] == "queued_w3_07_or_operator"
    assert built["builds"] is False and built["applied"] is False
    with pytest.raises(ValueError, match="adjacent"):
        plan_transition("research-api", "discovered", "active")
    with pytest.raises(ValueError, match="inspection evidence"):
        plan_transition("research-api", "inspected", "proposed")


def test_capability_wrappers_preserve_the_inert_boundary():
    from vera.integrations import integrations_capabilities as caps
    lifecycle = asyncio.run(caps.integration_source_lifecycle.__wrapped__())
    assert lifecycle == lifecycle_contract()
    inspected = asyncio.run(caps.integration_source_inspect.__wrapped__(
        kind="openapi", document=_openapi(), source_id="research-api"))
    assert inspected["accepted"] is True
    assert inspected["registers"] is False and inspected["network_io"] is False
    queued = asyncio.run(caps.integration_source_transition_plan.__wrapped__(
        source_id="research-api", current="proposed", target="built",
        evidence_refs=[inspected["inspection_id"]]))
    assert queued["allowed"] is False and queued["applied"] is False
    panel = (caps._HERE / "integrations_panel.html").read_text(encoding="utf-8")
    assert "/integrations/source/lifecycle" in panel
    assert "Source intake" in panel
    assert "MCP + OpenAPI inspection" in panel
    assert "build plans · execution queued" in panel
