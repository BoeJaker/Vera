"""Offline A2A v1.0 mapping and conformance contract; performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse


A2A_MAPPING_SCHEMA = "vera.a2a-protocol-mapping/v1"
A2A_PROTOCOL_VERSION = "1.0"
A2A_SDK_PACKAGE = "a2a-sdk==1.1.2"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+|=@-]{0,255}\Z")
_SECRET_KEYS = frozenset({
    "access_token", "api_key", "api_token", "apikey", "credential", "credentials",
    "password", "passwd", "refresh_token", "secret", "token",
})


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _text(value: Any, field_name: str, *, maximum: int = 1200) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _bounded_json(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("A2A document nesting exceeds 8 levels")
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise ValueError("A2A object exceeds 100 fields")
        for key, item in value.items():
            key = str(key)
            if len(key) > 128:
                raise ValueError("A2A field name exceeds 128 characters")
            if (_normalized_key(key) in _SECRET_KEYS and isinstance(item, str)
                    and item.strip()):
                raise ValueError("A2A documents must not contain plaintext credentials")
            _bounded_json(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise ValueError("A2A array exceeds 100 items")
        for item in value:
            _bounded_json(item, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("A2A documents must contain JSON values")


@dataclass(frozen=True)
class A2AStatusMapping:
    task_state: str
    run_status: str
    classification: str
    event_type: str
    lossless: bool

    def __post_init__(self) -> None:
        for name in ("task_state", "classification", "event_type"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.run_status:
            object.__setattr__(self, "run_status", _identifier(
                self.run_status, "Run status"))
        if self.classification not in {
                "unknown", "in_flight", "interrupted", "terminal"}:
            raise ValueError("unsupported A2A status classification")
        if not isinstance(self.lossless, bool):
            raise TypeError("lossless must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class A2AConformanceCase:
    case_id: str
    area: str
    expected: str
    lane: str = "deterministic"

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "case ID"))
        object.__setattr__(self, "area", _identifier(self.area, "case area"))
        object.__setattr__(self, "expected", _text(self.expected, "expected outcome"))
        object.__setattr__(self, "lane", _identifier(self.lane, "case lane"))
        if self.lane not in {"deterministic", "queued_live"}:
            raise ValueError("unsupported conformance lane")

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class A2AGap:
    code: str
    path: str
    detail: str
    blocking: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "gap code"))
        object.__setattr__(self, "path", _identifier(self.path, "gap path"))
        object.__setattr__(self, "detail", _text(self.detail, "gap detail"))
        if not isinstance(self.blocking, bool):
            raise TypeError("blocking must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class A2ASkillProjection:
    skill_id: str
    canonical_task: str
    input_modes: tuple[str, ...]
    output_modes: tuple[str, ...]
    security_required: bool
    effects_status: str = "unknown"
    authorized: bool = False
    executable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_id", _identifier(self.skill_id, "skill ID"))
        object.__setattr__(self, "canonical_task", _identifier(
            self.canonical_task, "canonical task"))
        for name in ("input_modes", "output_modes"):
            values = tuple(sorted({_text(item, name, maximum=128)
                                   for item in getattr(self, name)}))
            if not values:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, values)
        if self.effects_status != "unknown":
            raise ValueError("remote skill effects must remain unknown before trust review")
        if self.authorized or self.executable:
            raise ValueError("card projection cannot authorize or execute a remote skill")

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "input_modes": list(self.input_modes),
            "output_modes": list(self.output_modes),
            "provider": "a2a",
            "remote": True,
        }


@dataclass(frozen=True)
class A2ACardAnalysis:
    card_fingerprint: str
    protocol_version: str
    interface_binding: str
    interface_url_origin: str
    projections: tuple[A2ASkillProjection, ...]
    issues: tuple[str, ...]
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_fingerprint": self.card_fingerprint,
            "protocol_version": self.protocol_version,
            "interface_binding": self.interface_binding,
            "interface_url_origin": self.interface_url_origin,
            "projections": [item.to_dict() for item in self.projections],
            "issues": list(self.issues),
            "accepted": self.accepted,
            "authorized": False,
            "executes": False,
        }


@dataclass(frozen=True)
class A2AProtocolMapping:
    status_mapping: tuple[A2AStatusMapping, ...]
    identifier_mapping: tuple[tuple[str, str], ...]
    object_mapping: tuple[tuple[str, str], ...]
    operation_mapping: tuple[tuple[str, str], ...]
    security_invariants: tuple[str, ...]
    conformance_cases: tuple[A2AConformanceCase, ...]
    gaps: tuple[A2AGap, ...]
    schema: str = A2A_MAPPING_SCHEMA
    protocol_version: str = A2A_PROTOCOL_VERSION
    sdk_package: str = A2A_SDK_PACKAGE
    mapping_id: str = field(init=False)

    def __post_init__(self) -> None:
        statuses = tuple(sorted(self.status_mapping, key=lambda item: item.task_state))
        if not statuses or len({item.task_state for item in statuses}) != len(statuses):
            raise ValueError("A2A task status mappings must be non-empty and unique")
        object.__setattr__(self, "status_mapping", statuses)
        for field_name in ("identifier_mapping", "object_mapping", "operation_mapping"):
            pairs = tuple(sorted(getattr(self, field_name)))
            if not pairs or len({key for key, _ in pairs}) != len(pairs):
                raise ValueError(f"{field_name} must be non-empty with unique keys")
            for key, value in pairs:
                _identifier(key, field_name + " key")
                _identifier(value, field_name + " value")
            object.__setattr__(self, field_name, pairs)
        invariants = tuple(sorted({_identifier(item, "security invariant")
                                   for item in self.security_invariants}))
        if not invariants:
            raise ValueError("security invariants must not be empty")
        object.__setattr__(self, "security_invariants", invariants)
        cases = tuple(sorted(self.conformance_cases, key=lambda item: item.case_id))
        if not cases or len({item.case_id for item in cases}) != len(cases):
            raise ValueError("conformance cases must be non-empty and unique")
        object.__setattr__(self, "conformance_cases", cases)
        gaps = tuple(sorted(self.gaps, key=lambda item: (item.path, item.code)))
        if not gaps or len({(item.path, item.code) for item in gaps}) != len(gaps):
            raise ValueError("A2A gaps must be non-empty and unique")
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "mapping_id", "a2amap_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    @property
    def ready_for_execution(self) -> bool:
        return not any(item.blocking for item in self.gaps)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_version": self.protocol_version,
            "sdk_package": self.sdk_package,
            "status_mapping": [item.to_dict() for item in self.status_mapping],
            "identifier_mapping": dict(self.identifier_mapping),
            "object_mapping": dict(self.object_mapping),
            "operation_mapping": dict(self.operation_mapping),
            "security_invariants": list(self.security_invariants),
            "conformance_cases": [item.to_dict() for item in self.conformance_cases],
            "gaps": [item.to_dict() for item in self.gaps],
        }

    def to_dict(self) -> dict[str, Any]:
        lanes = {"deterministic": 0, "queued_live": 0}
        for case in self.conformance_cases:
            lanes[case.lane] += 1
        return {
            "mapping_id": self.mapping_id,
            **self.identity_dict(),
            "lanes": lanes,
            "ready_for_execution": self.ready_for_execution,
            "client_plan_contract": True,
            "server_plan_contract": True,
            "first_non_mutating_task_plan": True,
            "client_implemented": False,
            "server_implemented": False,
            "imports_runtime": False,
            "executes": False,
        }


def analyze_agent_card(
        card: Mapping[str, Any], *, supported_bindings: tuple[str, ...] = (
            "JSONRPC", "HTTP+JSON"), supported_extensions: tuple[str, ...] = (),
) -> A2ACardAnalysis:
    """Validate inline card structure and project skills without fetching its URL."""
    if not isinstance(card, Mapping):
        raise TypeError("card must be a mapping")
    _bounded_json(card)
    encoded = _canonical(card)
    if len(encoded.encode()) > 65536:
        raise ValueError("A2A card exceeds 65536 bytes")
    for name in ("name", "description", "version"):
        _text(card.get(name), "AgentCard." + name)
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list) or not interfaces:
        raise ValueError("AgentCard.supportedInterfaces must not be empty")
    bindings = {_identifier(item, "supported binding") for item in supported_bindings}
    selected = None
    issues: list[str] = []
    for interface in interfaces:
        if not isinstance(interface, Mapping):
            raise ValueError("AgentCard interface must be an object")
        binding = _identifier(interface.get("protocolBinding"), "protocol binding")
        version = _identifier(interface.get("protocolVersion"), "protocol version")
        parsed = urlparse(_text(interface.get("url"), "interface URL"))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise ValueError("A2A interface URL must be credential-free HTTPS")
        if version != A2A_PROTOCOL_VERSION:
            issues.append("unsupported_protocol_version:" + version)
            continue
        if binding not in bindings:
            issues.append("unsupported_protocol_binding:" + binding)
            continue
        if selected is None:
            selected = (binding, parsed)
    capabilities = card.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise ValueError("AgentCard.capabilities must be an object")
    extensions = capabilities.get("extensions") or []
    if not isinstance(extensions, list):
        raise ValueError("AgentCard.capabilities.extensions must be an array")
    supported = set(supported_extensions)
    for extension in extensions:
        if not isinstance(extension, Mapping):
            raise ValueError("AgentCard extension must be an object")
        uri = _text(extension.get("uri"), "extension URI", maximum=512)
        if extension.get("required") is True and uri not in supported:
            issues.append("required_extension_unsupported:" + uri)
    defaults_in = card.get("defaultInputModes")
    defaults_out = card.get("defaultOutputModes")
    if not isinstance(defaults_in, list) or not isinstance(defaults_out, list):
        raise ValueError("AgentCard default media modes must be arrays")
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ValueError("AgentCard.skills must not be empty")
    security_required = bool(card.get("securityRequirements"))
    projections = []
    for skill in skills:
        if not isinstance(skill, Mapping):
            raise ValueError("AgentCard skill must be an object")
        skill_id = _identifier(skill.get("id"), "skill ID")
        _text(skill.get("name"), "skill name")
        _text(skill.get("description"), "skill description")
        tags = skill.get("tags")
        if not isinstance(tags, list) or not tags:
            raise ValueError("AgentCard skill tags must not be empty")
        projections.append(A2ASkillProjection(
            skill_id=skill_id,
            canonical_task="a2a.skill/" + skill_id,
            input_modes=tuple(skill.get("inputModes") or defaults_in),
            output_modes=tuple(skill.get("outputModes") or defaults_out),
            security_required=security_required))
    if len({item.skill_id for item in projections}) != len(projections):
        raise ValueError("AgentCard skill IDs must be unique")
    if selected is None:
        issues.append("no_supported_interface")
        binding, origin = "", ""
    else:
        binding, parsed = selected
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            origin += f":{parsed.port}"
    return A2ACardAnalysis(
        card_fingerprint="sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
        protocol_version=A2A_PROTOCOL_VERSION,
        interface_binding=binding,
        interface_url_origin=origin,
        projections=tuple(sorted(projections, key=lambda item: item.skill_id)),
        issues=tuple(sorted(set(issues))), accepted=not issues)


def compile_a2a_protocol_mapping() -> A2AProtocolMapping:
    statuses = (
        A2AStatusMapping("TASK_STATE_UNSPECIFIED", "", "unknown",
                         "a2a.task.state_unknown", False),
        A2AStatusMapping("TASK_STATE_SUBMITTED", "queued", "in_flight",
                         "run.queued", True),
        A2AStatusMapping("TASK_STATE_WORKING", "running", "in_flight",
                         "run.running", True),
        A2AStatusMapping("TASK_STATE_INPUT_REQUIRED", "waiting", "interrupted",
                         "a2a.task.input_required", False),
        A2AStatusMapping("TASK_STATE_AUTH_REQUIRED", "waiting", "interrupted",
                         "a2a.task.auth_required", False),
        A2AStatusMapping("TASK_STATE_COMPLETED", "completed", "terminal",
                         "run.completed", True),
        A2AStatusMapping("TASK_STATE_FAILED", "failed", "terminal",
                         "run.failed", True),
        A2AStatusMapping("TASK_STATE_CANCELED", "cancelled", "terminal",
                         "run.cancelled", True),
        A2AStatusMapping("TASK_STATE_REJECTED", "failed", "terminal",
                         "a2a.task.rejected", False),
    )
    identifiers = (
        ("AgentCard.skill.id", "CapabilityContract.canonical_task|provider_identity"),
        ("Message.messageId", "RunEvent.causation_id|send_deduplication_hint"),
        ("Task.id", "Run.task_id|server_assigned_remote_id"),
        ("Task.contextId", "Run.policy.a2a_context_id|opaque_not_session_id"),
        ("Vera.Run.id", "local_authority_id|never_sent_as_new_Task.id"),
    )
    objects = (
        ("AgentCard", "CapabilityContract.v2.remote_candidate_manifest"),
        ("AgentSkill", "CapabilityContract.canonical_task_candidate"),
        ("Artifact", "ArtifactRef.after_verification_and_storage"),
        ("Message", "RunEvent.message_reference|content_out_of_band"),
        ("Part.data", "ArtifactRef.structured_record"),
        ("Part.raw", "ArtifactRef.checksummed_blob"),
        ("Part.text", "ArtifactRef.or_bounded_message_reference"),
        ("Part.url", "ArtifactRef.after_allowlist_fetch_and_checksum"),
        ("Task", "Run.remote_projection|native_A2A_authority"),
    )
    operations = (
        ("agentCard/get", "discovery_only|never_registration_authority"),
        ("message/send", "remote_task_request|Vera_policy_required"),
        ("message/stream", "remote_event_projection|bounded_order_validation"),
        ("tasks/cancel", "RunControl.cancel|remote_ack_required"),
        ("tasks/get", "remote_status_observation"),
        ("tasks/list", "bounded_paginated_observation"),
        ("tasks/resubscribe", "disconnect_resume|sequence_dedup_required"),
    )
    invariants = (
        "agent_card_size_and_depth_bounded", "authorization_remains_local",
        "credentials_out_of_band", "https_and_identity_verification_required",
        "malicious_metadata_never_authority", "plaintext_secrets_rejected",
        "remote_effects_unknown_until_review", "required_extensions_fail_closed",
        "ssrf_safe_artifact_and_webhook_policy", "untrusted_content_never_prompt",
    )
    cases = (
        A2AConformanceCase(
            "a2a.card.valid", "discovery",
            "A bounded v1.0 HTTPS card projects remote skills without registering or authorizing them."),
        A2AConformanceCase(
            "a2a.card.malicious_metadata", "security",
            "Plaintext credentials, excessive shape, unsupported required extensions, and unsafe URLs fail closed."),
        A2AConformanceCase(
            "a2a.status.complete", "task",
            "Every v1.0 task state has an explicit Run projection or an explicit semantic gap."),
        A2AConformanceCase(
            "a2a.ids.authority", "identity",
            "Server task/context IDs remain opaque and distinct from local Run/session identity."),
        A2AConformanceCase(
            "a2a.artifact.boundary", "artifact",
            "Remote parts become ArtifactRefs only after media, origin, size, checksum, and content checks."),
        A2AConformanceCase(
            "a2a.part.unsupported", "artifact",
            "Unknown part wrappers and required extensions fail closed before any ArtifactRef is created."),
        A2AConformanceCase(
            "a2a.send.non_mutating", "task",
            "One reviewed no-effect skill can be projected, but no request is sent in the deterministic lane.",
            "queued_live"),
        A2AConformanceCase(
            "a2a.send.duplicate", "idempotency",
            "Retry reuses messageId and proves remote behavior without assuming send is idempotent.",
            "queued_live"),
        A2AConformanceCase(
            "a2a.cancel.ack", "cancellation",
            "Repeated cancellation is correlated to a remote acknowledgement and terminal state.",
            "queued_live"),
        A2AConformanceCase(
            "a2a.stream.resume", "recovery",
            "Disconnect and resubscribe preserve task identity, ordering, deduplication, and terminal state.",
            "queued_live"),
        A2AConformanceCase(
            "a2a.auth.policy", "security",
            "Transport authentication and Vera side-effect policy both pass without exposing credentials.",
            "queued_live"),
        A2AConformanceCase(
            "a2a.teardown", "lifecycle",
            "Clients, streams, callbacks, task leases, and temporary artifacts are fully released.",
            "queued_live"),
    )
    gaps = (
        A2AGap("client_not_implemented", "client",
               "No A2A client transport, version negotiation, polling, streaming, or retry owner exists."),
        A2AGap("server_not_implemented", "server",
               "No Vera Agent Card or inbound A2A task server exists."),
        A2AGap("auth_not_proven", "security.authentication",
               "Credential resolution, TLS identity, tenant binding, and authenticated extended cards need live evidence."),
        A2AGap("policy_not_bound", "security.authorization",
               "Remote advertised skills and task content are not yet bound to trusted Vera policy context."),
        A2AGap("send_idempotency_optional", "operations.message/send",
               "A2A send may be idempotent; duplicate suppression must be measured using stable messageId."),
        A2AGap("stream_resume_unproven", "operations.tasks/resubscribe",
               "Ordering, duplicate events, disconnect recovery, and terminal convergence need a live server."),
        A2AGap("artifact_verification_missing", "objects.Artifact",
               "URI allowlists, SSRF controls, byte limits, checksums, media validation, storage, and cleanup are not implemented."),
        A2AGap("push_webhook_boundary_missing", "operations.push_notifications",
               "Webhook allowlisting, callback authentication, replay protection, and teardown are not implemented."),
        A2AGap("task_retention_unknown", "operations.tasks/get",
               "Remote retention and TaskNotFound behavior cannot be inferred from the protocol."),
    )
    return A2AProtocolMapping(
        status_mapping=statuses, identifier_mapping=identifiers,
        object_mapping=objects, operation_mapping=operations,
        security_invariants=invariants, conformance_cases=cases, gaps=gaps)
