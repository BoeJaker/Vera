"""Payload-free evidence boundary for optional validation providers.

Provider manifests describe effects and failure semantics, but never authorize
them.  Frozen projections contain only case identities, bounded violation codes,
exact record/revision citations, and usage accounting; Vera retains policy and
effect authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from .evaluation_evidence import (
    EvaluationCaseIdentity,
    EvaluationUsage,
    evaluation_case_identity_from_dict,
    evaluation_usage_from_dict,
)
from .model_package import _identifier
from ..fabric.retrieval_comparison import RetrievalCitation


MANIFEST_SCHEMA = "vera.validator-provider-manifest/v1"
PROJECTION_SCHEMA = "vera.validator-frozen-projection/v1"
REPORT_SCHEMA = "vera.validation-evidence/v1"
MAX_PROJECTION_BYTES = 8 * 1024 * 1024
MAX_CASES = 100_000
_EFFECTS = {"model", "network", "filesystem", "database", "process"}
_STAGES = {"input", "retrieval", "output", "artifact"}
_FORBIDDEN_KEYS = {
    "input", "inputs", "output", "outputs", "prompt", "prompts",
    "response", "responses", "message", "messages", "text", "content",
    "reason", "reasons", "error_detail", "traceback", "stacktrace",
    "secret", "secrets", "token", "api_key",
}


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {sorted(unknown)}")


def _scan_payload_keys(value: Any, path: str = "projection") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"projection keys must be strings at {path}")
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"payload-bearing field is forbidden at {path}.{key}")
            _scan_payload_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_payload_keys(item, f"{path}[{index}]")


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ValidatorSpec:
    validator_id: str
    validator_version: str
    stage: str
    effects: tuple[str, ...] = ()
    requested_failure_mode: str = "report"
    timeout_ms: int = 0
    model_package_id: str = ""
    egress_scope_digest: str = ""
    requires_secret_refs: bool = False

    def __post_init__(self) -> None:
        for name in ("validator_id", "validator_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.stage not in _STAGES:
            raise ValueError("unsupported validator stage")
        raw_effects = tuple(self.effects)
        if any(not isinstance(item, str) for item in raw_effects):
            raise ValueError("validator effects must be strings")
        effects = tuple(sorted(_identifier(item, "effect") for item in raw_effects))
        if len(set(effects)) != len(effects):
            raise ValueError("validator effects must be unique")
        if set(effects) - _EFFECTS:
            raise ValueError("unsupported validator effect")
        object.__setattr__(self, "effects", effects)
        if self.requested_failure_mode not in {"report", "block"}:
            raise ValueError("requested_failure_mode must be report or block")
        object.__setattr__(self, "timeout_ms", _nonnegative(self.timeout_ms, "timeout_ms"))
        if self.model_package_id:
            object.__setattr__(self, "model_package_id", _identifier(
                self.model_package_id, "model package ID"))
        if ("model" in effects) != bool(self.model_package_id):
            raise ValueError("model effects require exactly one model package ID")
        if self.egress_scope_digest:
            digest = str(self.egress_scope_digest).lower()
            if not (digest.startswith("sha256:") and len(digest) == 71 and
                    all(char in "0123456789abcdef" for char in digest[7:])):
                raise ValueError("egress_scope_digest must be sha256")
            object.__setattr__(self, "egress_scope_digest", digest)
        if ("network" in effects) != bool(self.egress_scope_digest):
            raise ValueError("network effects require exactly one egress scope digest")
        if not isinstance(self.requires_secret_refs, bool):
            raise ValueError("requires_secret_refs must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "stage": self.stage,
            "effects": list(self.effects),
            "requested_failure_mode": self.requested_failure_mode,
            "timeout_ms": self.timeout_ms,
            "model_package_id": self.model_package_id,
            "egress_scope_digest": self.egress_scope_digest,
            "requires_secret_refs": self.requires_secret_refs,
        }


@dataclass(frozen=True)
class ValidatorProviderManifest:
    provider_id: str
    provider_version: str
    validators: tuple[ValidatorSpec, ...]
    source_kind: str = "generic"
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANIFEST_SCHEMA:
            raise ValueError("unsupported validator manifest schema")
        for name in ("provider_id", "provider_version", "source_kind"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        validators = tuple(sorted(self.validators, key=lambda item: item.validator_id))
        if not validators or not all(isinstance(item, ValidatorSpec) for item in validators):
            raise ValueError("validators must contain ValidatorSpec values")
        if len({item.validator_id for item in validators}) != len(validators):
            raise ValueError("validator IDs must be unique")
        object.__setattr__(self, "validators", validators)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "source_kind": self.source_kind,
            "validators": [item.to_dict() for item in self.validators],
            "policy_authority": False,
        }


@dataclass(frozen=True)
class ValidationObservation:
    case: EvaluationCaseIdentity
    validator_id: str
    status: str
    violation_codes: tuple[str, ...] = ()
    citations: tuple[RetrievalCitation, ...] = ()
    usage: EvaluationUsage = field(default_factory=EvaluationUsage)
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.case, EvaluationCaseIdentity):
            raise TypeError("case must be EvaluationCaseIdentity")
        object.__setattr__(self, "validator_id", _identifier(
            self.validator_id, "validator ID"))
        if self.status not in {"passed", "violated", "failed", "skipped"}:
            raise ValueError("unsupported validation status")
        raw_violations = tuple(self.violation_codes)
        if any(not isinstance(item, str) for item in raw_violations):
            raise ValueError("violation codes must be strings")
        violations = tuple(sorted(_identifier(item, "violation code")
                                  for item in raw_violations))
        if len(set(violations)) != len(violations):
            raise ValueError("violation codes must be unique")
        object.__setattr__(self, "violation_codes", violations)
        citations = tuple(sorted(self.citations))
        if not all(isinstance(item, RetrievalCitation) for item in citations):
            raise TypeError("citations must contain RetrievalCitation values")
        if len(set(citations)) != len(citations):
            raise ValueError("citations must be unique")
        object.__setattr__(self, "citations", citations)
        if not isinstance(self.usage, EvaluationUsage):
            raise TypeError("usage must be EvaluationUsage")
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(
                self.error_code, "error code"))
        if self.status == "violated" and not violations:
            raise ValueError("violated observations require violation codes")
        if self.status != "violated" and violations:
            raise ValueError("only violated observations may contain violation codes")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed observations require an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed observations may contain an error code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id, "validator_id": self.validator_id,
            "status": self.status, "violation_codes": list(self.violation_codes),
            "citations": [item.to_dict() for item in self.citations],
            "usage": self.usage.to_dict(), "error_code": self.error_code,
        }


@dataclass(frozen=True)
class ValidationEvidenceReport:
    manifest: ValidatorProviderManifest
    expected_cases: tuple[EvaluationCaseIdentity, ...]
    observations: tuple[ValidationObservation, ...]
    status: str
    projection_digest: str
    report_id: str = field(init=False)
    schema: str = REPORT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ValidatorProviderManifest):
            raise TypeError("manifest must be ValidatorProviderManifest")
        cases = tuple(sorted(self.expected_cases, key=lambda item: item.case_id))
        if not cases or not all(isinstance(item, EvaluationCaseIdentity) for item in cases):
            raise ValueError("expected_cases cannot be empty")
        if len({item.case_id for item in cases}) != len(cases):
            raise ValueError("expected case IDs must be unique")
        object.__setattr__(self, "expected_cases", cases)
        observations = tuple(sorted(
            self.observations, key=lambda item: (item.case.case_id, item.validator_id)))
        if not observations or not all(
                isinstance(item, ValidationObservation) for item in observations):
            raise ValueError("observations cannot be empty")
        pairs = {(item.case.case_id, item.validator_id) for item in observations}
        if len(pairs) != len(observations):
            raise ValueError("case/validator observation pairs must be unique")
        expected_case_ids = {item.case_id for item in cases}
        expected_validator_ids = {item.validator_id for item in self.manifest.validators}
        if any(case_id not in expected_case_ids or validator_id not in expected_validator_ids
               for case_id, validator_id in pairs):
            raise ValueError("observations must reference expected cases and validators")
        object.__setattr__(self, "observations", observations)
        if self.status not in {"partial", "completed"}:
            raise ValueError("report status must be partial or completed")
        complete_pairs = {(case_id, validator_id) for case_id in expected_case_ids
                          for validator_id in expected_validator_ids}
        if self.status == "completed" and pairs != complete_pairs:
            raise ValueError("completed reports require every case/validator pair")
        if self.status == "partial" and not pairs < complete_pairs:
            raise ValueError("partial reports require a proper observation subset")
        digest = str(self.projection_digest or "").lower()
        if not (digest.startswith("sha256:") and len(digest) == 71 and
                all(char in "0123456789abcdef" for char in digest[7:])):
            raise ValueError("projection_digest must be sha256")
        object.__setattr__(self, "projection_digest", digest)
        identity = json.dumps(self.identity_dict(), sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False).encode()
        object.__setattr__(self, "report_id", "vrep_" + hashlib.sha256(identity).hexdigest())

    @property
    def violated(self) -> bool:
        return any(item.status == "violated" for item in self.observations)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "manifest": self.manifest.to_dict(),
            "expected_case_ids": [item.case_id for item in self.expected_cases],
            "observations": [item.to_dict() for item in self.observations],
            "status": self.status, "projection_digest": self.projection_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "violated": self.violated,
            "policy_authority": False, "effect": "none",
            "provider_invoked": False, "payloads_retained": False,
            **self.identity_dict(),
        }


def _validator(value: Mapping[str, Any]) -> ValidatorSpec:
    if not isinstance(value, Mapping):
        raise ValueError("validator entries must be objects")
    _keys(value, {"validator_id", "validator_version", "stage", "effects",
                  "requested_failure_mode", "timeout_ms", "model_package_id",
                  "egress_scope_digest", "requires_secret_refs"}, "validator")
    try:
        return ValidatorSpec(
            validator_id=value["validator_id"],
            validator_version=value["validator_version"], stage=value["stage"],
            effects=tuple(value.get("effects") or ()),
            requested_failure_mode=value.get("requested_failure_mode", "report"),
            timeout_ms=value.get("timeout_ms", 0),
            model_package_id=value.get("model_package_id", ""),
            egress_scope_digest=value.get("egress_scope_digest", ""),
            requires_secret_refs=value.get("requires_secret_refs", False),
        )
    except KeyError as exc:
        raise ValueError("validator entry is missing a required field") from exc


def _manifest(value: Mapping[str, Any]) -> ValidatorProviderManifest:
    if not isinstance(value, Mapping):
        raise ValueError("manifest must be an object")
    _keys(value, {"schema", "provider_id", "provider_version", "source_kind",
                  "validators", "policy_authority"}, "manifest")
    if value.get("policy_authority") not in (None, False):
        raise ValueError("external validators cannot claim policy authority")
    try:
        return ValidatorProviderManifest(
            provider_id=value["provider_id"],
            provider_version=value["provider_version"],
            source_kind=value.get("source_kind", "generic"),
            validators=tuple(_validator(item) for item in value["validators"]),
            schema=value.get("schema", ""),
        )
    except KeyError as exc:
        raise ValueError("manifest is missing a required field") from exc


def _observation(value: Mapping[str, Any], *,
                 cases: Mapping[str, EvaluationCaseIdentity]) -> ValidationObservation:
    if not isinstance(value, Mapping):
        raise ValueError("observation entries must be objects")
    _keys(value, {"case_id", "validator_id", "status", "violation_codes",
                  "citations", "usage", "error_code"}, "observation")
    case_id = str(value.get("case_id") or "")
    if case_id not in cases:
        raise ValueError("observation case ID is not expected")
    usage = value.get("usage") or {}
    if not isinstance(usage, Mapping):
        raise ValueError("observation usage must be an object")
    _keys(usage, {"input_tokens", "output_tokens", "cost_microunits", "latency_ms"},
          "observation usage")
    raw_citations = value.get("citations") or ()
    for citation in raw_citations:
        if not isinstance(citation, Mapping):
            raise ValueError("citation entries must be objects")
        _keys(citation, {"record_id", "revision_id"}, "citation")
    return ValidationObservation(
        case=cases[case_id], validator_id=value.get("validator_id", ""),
        status=value.get("status", ""),
        violation_codes=tuple(value.get("violation_codes") or ()),
        citations=tuple(RetrievalCitation(**item) for item in raw_citations),
        usage=evaluation_usage_from_dict(usage),
        error_code=value.get("error_code", ""),
    )


def import_validator_projection(projection: Mapping[str, Any]) -> ValidationEvidenceReport:
    """Import already-produced safe evidence; never loads or invokes a provider."""
    if not isinstance(projection, Mapping):
        raise TypeError("projection must be an object")
    _scan_payload_keys(projection)
    _keys(projection, {"schema", "manifest", "expected_cases", "observations"},
          "projection")
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise ValueError("validator projection exceeds byte limit")
    if projection.get("schema") != PROJECTION_SCHEMA:
        raise ValueError("unsupported validator projection schema")
    manifest = _manifest(projection.get("manifest") or {})
    raw_cases = projection.get("expected_cases")
    raw_observations = projection.get("observations")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)) or not (
            0 < len(raw_cases) <= MAX_CASES):
        raise ValueError("expected_cases must contain 1..100000 entries")
    if not isinstance(raw_observations, Sequence) or isinstance(
            raw_observations, (str, bytes)) or not raw_observations:
        raise ValueError("observations cannot be empty")
    cases = tuple(evaluation_case_identity_from_dict(item) for item in raw_cases)
    by_id = {item.case_id: item for item in cases}
    if len(by_id) != len(cases):
        raise ValueError("expected case identities must be unique")
    observations = tuple(_observation(item, cases=by_id) for item in raw_observations)
    expected_count = len(cases) * len(manifest.validators)
    status = "completed" if len(observations) == expected_count else "partial"
    return ValidationEvidenceReport(
        manifest=manifest, expected_cases=cases, observations=observations,
        status=status,
        projection_digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
    )


def import_guardrails_projection(projection: Mapping[str, Any]) -> ValidationEvidenceReport:
    manifest = projection.get("manifest") if isinstance(projection, Mapping) else None
    if not isinstance(manifest, Mapping) or manifest.get("source_kind") != "guardrails":
        raise ValueError("Guardrails projection requires source_kind guardrails")
    return import_validator_projection(projection)
