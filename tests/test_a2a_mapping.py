import asyncio
import sys

import pytest

from vera.execution.a2a_mapping import (
    A2A_PROTOCOL_VERSION, A2A_SDK_PACKAGE, A2AConformanceCase,
    A2AProtocolMapping, A2AStatusMapping, analyze_agent_card,
    compile_a2a_protocol_mapping)


pytestmark = pytest.mark.critical


def _card(**changes):
    card = {
        "name": "Read-only Research Agent",
        "description": "Finds public references without performing mutations.",
        "version": "2026.08",
        "supportedInterfaces": [{
            "url": "https://agent.example/a2a/v1",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }],
        "capabilities": {"streaming": True, "extensions": []},
        "securitySchemes": {"oauth": {"type": "oauth2"}},
        "securityRequirements": [{"oauth": ["research.read"]}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": "research.read",
            "name": "Research",
            "description": "Return public references.",
            "tags": ["research", "read"],
        }],
    }
    card.update(changes)
    return card


def test_manifest_is_stable_pinned_and_never_imports_or_executes_sdk():
    before = {name for name in sys.modules
              if name == "a2a" or name.startswith("a2a.")}
    first = compile_a2a_protocol_mapping()
    assert first == compile_a2a_protocol_mapping()
    assert first.protocol_version == A2A_PROTOCOL_VERSION == "1.0"
    assert first.sdk_package == A2A_SDK_PACKAGE == "a2a-sdk==1.1.2"
    assert first.mapping_id.startswith("a2amap_")
    after = {name for name in sys.modules
             if name == "a2a" or name.startswith("a2a.")}
    assert after == before == set()
    result = first.to_dict()
    assert result["imports_runtime"] is False
    assert result["executes"] is False
    assert result["client_implemented"] is False
    assert result["server_implemented"] is False
    assert result["ready_for_execution"] is False


def test_every_a2a_v1_task_state_is_explicit_and_unknown_is_not_coerced():
    mapping = {item.task_state: item for item in
               compile_a2a_protocol_mapping().status_mapping}
    assert set(mapping) == {
        "TASK_STATE_UNSPECIFIED", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
        "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
    assert mapping["TASK_STATE_UNSPECIFIED"].run_status == ""
    assert mapping["TASK_STATE_UNSPECIFIED"].classification == "unknown"
    assert mapping["TASK_STATE_UNSPECIFIED"].lossless is False
    assert mapping["TASK_STATE_AUTH_REQUIRED"].run_status == "waiting"
    assert mapping["TASK_STATE_AUTH_REQUIRED"].event_type == "a2a.task.auth_required"
    assert mapping["TASK_STATE_REJECTED"].run_status == "failed"
    assert mapping["TASK_STATE_REJECTED"].lossless is False


def test_identity_and_object_mappings_preserve_remote_authority_boundaries():
    manifest = compile_a2a_protocol_mapping()
    identifiers = dict(manifest.identifier_mapping)
    objects = dict(manifest.object_mapping)
    assert identifiers["Task.id"] == "Run.task_id|server_assigned_remote_id"
    assert identifiers["Task.contextId"].endswith("opaque_not_session_id")
    assert identifiers["Vera.Run.id"].endswith("never_sent_as_new_Task.id")
    assert objects["Task"] == "Run.remote_projection|native_A2A_authority"
    assert objects["Artifact"] == "ArtifactRef.after_verification_and_storage"
    assert objects["Part.url"].endswith("allowlist_fetch_and_checksum")


def test_valid_card_projects_candidates_without_registering_or_authorizing():
    first = analyze_agent_card(_card())
    second = analyze_agent_card(_card())
    assert first == second
    assert first.accepted is True
    assert first.issues == ()
    assert first.interface_binding == "JSONRPC"
    assert first.interface_url_origin == "https://agent.example"
    projection = first.to_dict()["projections"][0]
    assert projection == {
        "skill_id": "research.read",
        "canonical_task": "a2a.skill/research.read",
        "input_modes": ["text/plain"],
        "output_modes": ["application/json"],
        "security_required": True,
        "effects_status": "unknown",
        "authorized": False,
        "executable": False,
        "provider": "a2a",
        "remote": True,
    }
    assert first.to_dict()["executes"] is False


def test_interface_preference_version_and_required_extensions_fail_closed():
    card = _card(
        supportedInterfaces=[
            {"url": "https://agent.example/grpc", "protocolBinding": "GRPC",
             "protocolVersion": "1.0"},
            {"url": "https://agent.example/json", "protocolBinding": "HTTP+JSON",
             "protocolVersion": "1.0"},
        ],
        capabilities={"extensions": [{
            "uri": "https://extensions.example/unsafe/v1", "required": True}]})
    analysis = analyze_agent_card(card)
    assert not analysis.accepted
    assert analysis.interface_binding == "HTTP+JSON"
    assert analysis.issues == (
        "required_extension_unsupported:https://extensions.example/unsafe/v1",
        "unsupported_protocol_binding:GRPC",
    )
    accepted = analyze_agent_card(
        card, supported_bindings=("GRPC", "HTTP+JSON"),
        supported_extensions=("https://extensions.example/unsafe/v1",))
    assert accepted.accepted
    assert accepted.interface_binding == "GRPC"


@pytest.mark.parametrize("url", [
    "http://agent.example/a2a", "https://user:pass@agent.example/a2a",
    "file:///tmp/card", "not-a-url",
])
def test_unsafe_or_credential_bearing_interface_urls_are_rejected(url):
    card = _card(supportedInterfaces=[{
        "url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}])
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        analyze_agent_card(card)


def test_plaintext_credentials_and_unbounded_metadata_are_rejected():
    oauth_descriptor = _card(securitySchemes={"oauth": {
        "type": "oauth2", "tokenUrl": "https://identity.example/token",
        "authorizationUrl": "https://identity.example/authorize",
    }})
    assert analyze_agent_card(oauth_descriptor).accepted
    with pytest.raises(ValueError, match="plaintext credentials"):
        analyze_agent_card(_card(metadata={"api_token": "plaintext"}))
    with pytest.raises(ValueError, match="exceeds 100 fields"):
        analyze_agent_card(_card(metadata={str(i): i for i in range(101)}))
    nested = value = {}
    for _ in range(9):
        value["next"] = {}
        value = value["next"]
    with pytest.raises(ValueError, match="nesting exceeds"):
        analyze_agent_card(_card(metadata=nested))


def test_duplicate_skills_and_malformed_shapes_fail_instead_of_guessing():
    skill = _card()["skills"][0]
    with pytest.raises(ValueError, match="skill IDs must be unique"):
        analyze_agent_card(_card(skills=[skill, skill]))
    with pytest.raises(ValueError, match="default media modes"):
        analyze_agent_card(_card(defaultInputModes="text/plain"))
    with pytest.raises(ValueError, match="tags must not be empty"):
        analyze_agent_card(_card(skills=[{**skill, "tags": []}]))


def test_conformance_matrix_covers_all_roadmap_gates_and_separates_live_lane():
    manifest = compile_a2a_protocol_mapping()
    cases = {item.case_id: item for item in manifest.conformance_cases}
    assert set(cases) == {
        "a2a.card.valid", "a2a.card.malicious_metadata", "a2a.status.complete",
        "a2a.ids.authority", "a2a.artifact.boundary", "a2a.part.unsupported",
        "a2a.send.non_mutating",
        "a2a.send.duplicate", "a2a.cancel.ack", "a2a.stream.resume",
        "a2a.auth.policy", "a2a.teardown",
    }
    assert {item.lane for item in cases.values()} == {"deterministic", "queued_live"}
    assert manifest.to_dict()["lanes"] == {"deterministic": 6, "queued_live": 6}
    assert dict(manifest.operation_mapping)["message/send"].endswith(
        "Vera_policy_required")
    assert "send_idempotency_optional" in {gap.code for gap in manifest.gaps}


def test_record_types_reject_duplicate_statuses_cases_and_invalid_lanes():
    status = A2AStatusMapping(
        "TASK_STATE_COMPLETED", "completed", "terminal", "run.completed", True)
    case = A2AConformanceCase("case", "area", "expected")
    manifest = compile_a2a_protocol_mapping()
    values = manifest.__dict__.copy()
    values.pop("mapping_id")
    values["status_mapping"] = (status, status)
    with pytest.raises(ValueError, match="status mappings"):
        A2AProtocolMapping(**values)
    with pytest.raises(ValueError, match="unsupported conformance lane"):
        A2AConformanceCase("case", "area", "expected", "live_now")


def test_capability_returns_same_static_manifest():
    from vera import capability_orchestration as orchestration
    result = asyncio.run(orchestration.cap_interop_a2a_conformance.__wrapped__())
    assert result == compile_a2a_protocol_mapping().to_dict()
    assert result["ready_for_execution"] is False
    assert result["executes"] is False
