"""Payload-safe, offline conformance checks for inference transcripts."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .inference_contracts import InferenceTranscript
from .model_package import _identifier

INFERENCE_CONFORMANCE_SCHEMA = "vera.inference-conformance-expectation/v1"


def _output_digest(output) -> str:
    if output.artifact is None:
        value = output.to_dict()
    else:
        # Artifact location is transport, while its checksum is content identity.
        value = {"name": output.name, "media_type": output.media_type,
                 "value": None,
                 "artifact": {"sha256": output.artifact.sha256,
                              "size_bytes": output.artifact.size_bytes,
                              "media_type": output.artifact.media_type}}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InferenceConformanceExpectation:
    case_id: str
    request_id: str
    model_package_id: str
    status: str
    output_digests: tuple[str, ...] = ()
    error_code: str = ""
    usage: tuple[tuple[str, int], ...] = ()
    expectation_id: str = field(init=False)
    schema: str = INFERENCE_CONFORMANCE_SCHEMA

    def __post_init__(self) -> None:
        for name in ("case_id", "request_id", "model_package_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported expected inference status")
        digests = tuple(self.output_digests)
        if len(digests) > 10_000 or any(
                not isinstance(value, str) or len(value) != 71
                or not value.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in value[7:])
                for value in digests):
            raise ValueError("output digests must be bounded canonical sha256 values")
        object.__setattr__(self, "output_digests", digests)
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(
                self.error_code, "expected error code"))
        if (self.status == "failed") != bool(self.error_code):
            raise ValueError("only failed expectations require an error code")
        usage = tuple(sorted(self.usage))
        if len({key for key, _ in usage}) != len(usage) or any(
                _identifier(key, "usage key") != key
                or isinstance(value, bool) or not isinstance(value, int) or value < 0
                for key, value in usage):
            raise ValueError("expected usage must contain unique non-negative counters")
        object.__setattr__(self, "usage", usage)
        canonical = json.dumps(self.identity_dict(), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        object.__setattr__(self, "expectation_id", "iconf_" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest())

    @classmethod
    def from_transcript(cls, case_id: str, transcript: InferenceTranscript,
                        *, compare_usage: bool = True
                        ) -> "InferenceConformanceExpectation":
        if not isinstance(transcript, InferenceTranscript):
            raise TypeError("transcript must be InferenceTranscript")
        return cls(
            case_id, transcript.request_id, transcript.model_package_id,
            transcript.status,
            tuple(_output_digest(output) for output in transcript.outputs),
            transcript.error_code, transcript.usage if compare_usage else ())

    def identity_dict(self) -> dict:
        return {
            "schema": self.schema, "case_id": self.case_id,
            "request_id": self.request_id,
            "model_package_id": self.model_package_id,
            "status": self.status, "output_digests": list(self.output_digests),
            "error_code": self.error_code, "usage": dict(self.usage),
        }

    def to_dict(self) -> dict:
        return {"expectation_id": self.expectation_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class InferenceConformanceReport:
    expectation_id: str
    provider_id: str
    passed: bool
    mismatches: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"expectation_id": self.expectation_id,
                "provider_id": self.provider_id, "passed": self.passed,
                "mismatches": list(self.mismatches)}


def evaluate_inference_conformance(
        expectation: InferenceConformanceExpectation,
        transcript: InferenceTranscript) -> InferenceConformanceReport:
    """Compare supplied evidence; never dispatch inference or expose payloads."""
    if not isinstance(expectation, InferenceConformanceExpectation):
        raise TypeError("expectation must be InferenceConformanceExpectation")
    if not isinstance(transcript, InferenceTranscript):
        raise TypeError("transcript must be InferenceTranscript")
    actual = {
        "request_id": transcript.request_id,
        "model_package_id": transcript.model_package_id,
        "status": transcript.status,
        "output_digests": tuple(_output_digest(value)
                                for value in transcript.outputs),
        "error_code": transcript.error_code,
        "usage": transcript.usage,
    }
    expected = {
        "request_id": expectation.request_id,
        "model_package_id": expectation.model_package_id,
        "status": expectation.status,
        "output_digests": expectation.output_digests,
        "error_code": expectation.error_code,
    }
    mismatches = [name for name, value in expected.items()
                  if actual[name] != value]
    if expectation.usage and actual["usage"] != expectation.usage:
        mismatches.append("usage")
    return InferenceConformanceReport(
        expectation.expectation_id,
        _identifier(transcript.provider_id, "provider ID"), not mismatches,
        tuple(mismatches))


def inference_conformance_expectation_from_dict(
        value: Mapping[str, Any]) -> InferenceConformanceExpectation:
    if not isinstance(value, Mapping) or value.get("schema") != \
            INFERENCE_CONFORMANCE_SCHEMA:
        raise ValueError("unsupported inference conformance expectation schema")
    try:
        expectation = InferenceConformanceExpectation(
            case_id=value["case_id"], request_id=value["request_id"],
            model_package_id=value["model_package_id"], status=value["status"],
            output_digests=tuple(value.get("output_digests") or ()),
            error_code=value.get("error_code", ""),
            usage=tuple(dict(value.get("usage") or {}).items()))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference conformance expectation") from exc
    if value.get("expectation_id") != expectation.expectation_id:
        raise ValueError("inference conformance identity does not match content")
    return expectation
