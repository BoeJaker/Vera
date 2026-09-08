"""Provider-neutral, ModelPackage-bound inference contracts."""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Protocol, runtime_checkable

from .training_contracts import ProviderProfile

INFERENCE_REQUEST_SCHEMA = "vera.inference-request/v1"
INFERENCE_EVENT_SCHEMA = "vera.inference-event/v1"
MAX_INLINE_VALUE_BYTES = 262_144
MAX_REQUEST_BYTES = 1_048_576
MAX_INFERENCE_EVENTS = 10_000
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a bounded identifier")
    value = value.strip()
    if not _IDENT.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _canonical(value: Any, name: str, *, limit: int) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds its byte limit")
    return encoded


@dataclass(frozen=True, slots=True)
class InferenceArtifact:
    uri: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip() or len(self.uri) > 4096:
            raise ValueError("inference artifact URI must be a bounded string")
        object.__setattr__(self, "uri", self.uri.strip())
        digest = str(self.sha256 or "").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("inference artifact sha256 must be canonical")
        object.__setattr__(self, "sha256", digest.removeprefix("sha256:"))
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) \
                or self.size_bytes < 0:
            raise ValueError("inference artifact size must be non-negative")
        object.__setattr__(self, "media_type", _identifier(
            self.media_type, "inference artifact media type"))

    def to_dict(self) -> dict:
        return {"uri": self.uri, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "media_type": self.media_type}


@dataclass(frozen=True, slots=True)
class InferenceValue:
    name: str
    media_type: str
    json_data: str = ""
    artifact: InferenceArtifact | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "inference value name"))
        object.__setattr__(self, "media_type", _identifier(
            self.media_type, "inference value media type"))
        if bool(self.json_data) == bool(self.artifact):
            raise ValueError("inference value requires exactly one inline value or artifact")
        if self.json_data:
            if not isinstance(self.json_data, str):
                raise ValueError("inline inference value must be canonical JSON")
            try:
                decoded = json.loads(self.json_data)
            except (TypeError, ValueError) as exc:
                raise ValueError("inline inference value must be canonical JSON") from exc
            canonical = _canonical(decoded, "inline inference value",
                                   limit=MAX_INLINE_VALUE_BYTES)
            if canonical != self.json_data:
                raise ValueError("inline inference value must be canonical JSON")
        elif not isinstance(self.artifact, InferenceArtifact):
            raise TypeError("inference artifact value is invalid")

    @classmethod
    def from_json(cls, name: str, value: Any, *,
                  media_type: str = "application/json") -> "InferenceValue":
        return cls(name, media_type,
                   _canonical(value, "inline inference value",
                              limit=MAX_INLINE_VALUE_BYTES))

    @property
    def size_bytes(self) -> int:
        if self.artifact is not None:
            return self.artifact.size_bytes
        return len(self.json_data.encode("utf-8"))

    def to_dict(self) -> dict:
        return {"name": self.name, "media_type": self.media_type,
                "value": json.loads(self.json_data) if self.json_data else None,
                "artifact": self.artifact.to_dict() if self.artifact else None}


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    model_package_id: str
    task: str
    input_contract: str
    output_contract: str
    inputs: tuple[InferenceValue, ...]
    parameters: tuple[tuple[str, Any], ...] = ()
    stream: bool = False
    max_output_bytes: int = 1_048_576
    request_id: str = field(init=False)
    schema: str = INFERENCE_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        for name in ("model_package_id", "task", "input_contract", "output_contract"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        values = tuple(self.inputs)
        if not values or len(values) > 256 or not all(
                isinstance(value, InferenceValue) for value in values):
            raise ValueError("inference inputs must contain 1..256 values")
        if len({value.name for value in values}) != len(values):
            raise ValueError("inference input names must be unique")
        object.__setattr__(self, "inputs", tuple(sorted(values, key=lambda value: value.name)))
        try:
            parameter_pairs = tuple(self.parameters)
            raw_parameters = dict(parameter_pairs)
        except (TypeError, ValueError) as exc:
            raise ValueError("inference parameters must be key/value pairs") from exc
        if len(raw_parameters) != len(parameter_pairs) or len(raw_parameters) > 128:
            raise ValueError("inference parameter keys must be unique and bounded")
        parameters_json = _canonical(raw_parameters, "inference parameters",
                                     limit=MAX_INLINE_VALUE_BYTES)
        parameters = tuple(sorted(json.loads(parameters_json).items()))
        if any(not _IDENT.fullmatch(str(key)) for key, _ in parameters):
            raise ValueError("inference parameter keys must be bounded identifiers")
        if any(not isinstance(value, (str, int, float, bool, type(None)))
               for _, value in parameters):
            raise ValueError("inference parameter values must be JSON scalars")
        object.__setattr__(self, "parameters", parameters)
        if not isinstance(self.stream, bool):
            raise ValueError("inference stream must be boolean")
        if isinstance(self.max_output_bytes, bool) or not isinstance(
                self.max_output_bytes, int) or not 1 <= self.max_output_bytes <= 1_073_741_824:
            raise ValueError("max_output_bytes must be a positive bounded integer")
        identity_json = _canonical(
            self.identity_dict(), "inference request", limit=MAX_REQUEST_BYTES)
        digest = hashlib.sha256(identity_json.encode()).hexdigest()
        object.__setattr__(self, "request_id", f"ireq_{digest}")

    def identity_dict(self) -> dict:
        return {"schema": self.schema, "model_package_id": self.model_package_id,
                "task": self.task, "input_contract": self.input_contract,
                "output_contract": self.output_contract,
                "inputs": [value.to_dict() for value in self.inputs],
                "parameters": dict(self.parameters), "stream": self.stream,
                "max_output_bytes": self.max_output_bytes}

    def to_dict(self) -> dict:
        return {"request_id": self.request_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class InferenceEvent:
    request_id: str
    model_package_id: str
    provider_id: str
    sequence: int
    kind: str
    output: InferenceValue | None = None
    usage: tuple[tuple[str, int], ...] = ()
    error_code: str = ""
    schema: str = INFERENCE_EVENT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("request_id", "model_package_id", "provider_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) \
                or self.sequence < 0:
            raise ValueError("inference event sequence must be non-negative")
        if self.kind not in {"output", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported inference event kind")
        terminal = self.kind != "output"
        if (self.output is None) == (not terminal):
            raise ValueError("only output events carry an inference value")
        if self.output is not None and not isinstance(self.output, InferenceValue):
            raise TypeError("inference output value is invalid")
        try:
            usage = tuple(sorted((
                _identifier(key, "usage key"), value)
                for key, value in self.usage))
        except (TypeError, ValueError) as exc:
            raise ValueError("inference usage must be key/value pairs") from exc
        if len({key for key, _ in usage}) != len(usage) or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for _, value in usage):
            raise ValueError("inference usage must have unique non-negative counters")
        object.__setattr__(self, "usage", usage)
        if self.kind == "output" and usage:
            raise ValueError("only terminal inference events carry usage")
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(
                self.error_code, "inference error code"))
        if self.kind == "failed" and not self.error_code:
            raise ValueError("failed inference requires an error code")
        if self.kind != "failed" and self.error_code:
            raise ValueError("only failed inference carries an error code")

    def to_dict(self) -> dict:
        return {"schema": self.schema, "request_id": self.request_id,
                "model_package_id": self.model_package_id,
                "provider_id": self.provider_id, "sequence": self.sequence,
                "kind": self.kind,
                "output": self.output.to_dict() if self.output else None,
                "usage": dict(self.usage), "error_code": self.error_code}


class InferenceCancellation(Protocol):
    def checkpoint(self) -> None: ...


@runtime_checkable
class InferenceProvider(Protocol):
    def profile(self) -> ProviderProfile: ...
    def infer(self, request: InferenceRequest, *,
              cancellation: InferenceCancellation | None = None
              ) -> AsyncIterator[InferenceEvent]: ...


@dataclass(frozen=True, slots=True)
class InferenceTranscript:
    request_id: str
    provider_id: str
    model_package_id: str
    outputs: tuple[InferenceValue, ...]
    status: str
    usage: tuple[tuple[str, int], ...]
    error_code: str = ""

    def to_dict(self) -> dict:
        return {"request_id": self.request_id, "provider_id": self.provider_id,
                "model_package_id": self.model_package_id,
                "outputs": [value.to_dict() for value in self.outputs],
                "status": self.status, "usage": dict(self.usage),
                "error_code": self.error_code}


class InferenceContractConflict(ValueError):
    pass


def inference_value_from_dict(value: Mapping[str, Any]) -> InferenceValue:
    if not isinstance(value, Mapping):
        raise ValueError("malformed inference value")
    try:
        artifact_value = value.get("artifact")
        if artifact_value is not None and value.get("value") is not None:
            raise ValueError("inference value cannot contain inline and artifact payloads")
        artifact = InferenceArtifact(**artifact_value) if artifact_value else None
        if artifact is not None:
            return InferenceValue(value["name"], value["media_type"], artifact=artifact)
        return InferenceValue.from_json(
            value["name"], value["value"], media_type=value["media_type"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference value") from exc


def inference_request_from_dict(value: Mapping[str, Any]) -> InferenceRequest:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_REQUEST_SCHEMA:
        raise ValueError("unsupported inference request schema")
    try:
        request = InferenceRequest(
            model_package_id=value["model_package_id"], task=value["task"],
            input_contract=value["input_contract"],
            output_contract=value["output_contract"],
            inputs=tuple(inference_value_from_dict(item) for item in value["inputs"]),
            parameters=tuple(dict(value.get("parameters") or {}).items()),
            stream=value.get("stream", False),
            max_output_bytes=value["max_output_bytes"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference request") from exc
    if value.get("request_id") != request.request_id:
        raise InferenceContractConflict(
            "inference request identity does not match content")
    return request


def inference_event_from_dict(value: Mapping[str, Any]) -> InferenceEvent:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_EVENT_SCHEMA:
        raise ValueError("unsupported inference event schema")
    try:
        output = (inference_value_from_dict(value["output"])
                  if value.get("output") is not None else None)
        return InferenceEvent(
            request_id=value["request_id"],
            model_package_id=value["model_package_id"],
            provider_id=value["provider_id"], sequence=value["sequence"],
            kind=value["kind"], output=output,
            usage=tuple(dict(value.get("usage") or {}).items()),
            error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference event") from exc


async def consume_inference(
        provider: InferenceProvider, request: InferenceRequest, *,
        cancellation: InferenceCancellation | None = None) -> InferenceTranscript:
    """Validate one provider stream without retrying or interpreting payloads."""
    if not isinstance(request, InferenceRequest):
        raise TypeError("request must be InferenceRequest")
    profile = provider.profile()
    if not isinstance(profile, ProviderProfile) or profile.kind != "inference":
        raise InferenceContractConflict("provider profile is not inference")
    if request.task not in profile.capabilities:
        raise InferenceContractConflict("provider does not declare the requested task")
    if cancellation is not None:
        cancellation.checkpoint()
    stream = provider.infer(request, cancellation=cancellation)
    if not hasattr(stream, "__aiter__"):
        raise InferenceContractConflict("provider did not return an async event stream")
    outputs: list[InferenceValue] = []
    terminal: InferenceEvent | None = None
    output_bytes = 0
    expected_sequence = 0
    async for event in stream:
        if cancellation is not None:
            cancellation.checkpoint()
        if not isinstance(event, InferenceEvent):
            raise InferenceContractConflict("provider returned an invalid event")
        if terminal is not None:
            raise InferenceContractConflict("provider emitted after a terminal event")
        if event.sequence != expected_sequence:
            raise InferenceContractConflict("inference event sequence is not contiguous")
        expected_sequence += 1
        if expected_sequence > MAX_INFERENCE_EVENTS:
            raise InferenceContractConflict("inference event limit exceeded")
        if (event.request_id != request.request_id or
                event.model_package_id != request.model_package_id or
                event.provider_id != profile.provider_id):
            raise InferenceContractConflict("inference event identity mismatch")
        if event.kind == "output":
            output_bytes += event.output.size_bytes
            if output_bytes > request.max_output_bytes:
                raise InferenceContractConflict("inference output budget exceeded")
            outputs.append(event.output)
        else:
            terminal = event
    if terminal is None:
        raise InferenceContractConflict("provider stream ended without a terminal event")
    return InferenceTranscript(
        request_id=request.request_id, provider_id=profile.provider_id,
        model_package_id=request.model_package_id, outputs=tuple(outputs),
        status=terminal.kind, usage=terminal.usage,
        error_code=terminal.error_code)
