"""Payload-free evidence for a native Memory/Fabric dual-write attempt.

This module records outcomes; it performs no write and grants no authority.
That separation lets the later canary bind the mutable native identity to an
exact immutable revision without allowing a receipt constructor to become a
second transaction coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from .record_revision import RecordRevision


SCHEMA = "vera.memory-dual-write-receipt/v2"
OUTCOMES = frozenset({"committed", "failed", "timed_out", "cancelled", "skipped"})
NATIVE_BACKENDS = ("postgres", "chroma", "neo4j")
PRODUCER_KINDS = frozenset({
    "public_capability", "conversation", "capability_activity", "workflow",
    "automatic_promotion", "ide", "skill", "context_stream", "internal",
})
MAX_NATIVE_BACKENDS = 32
_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255}$")
_ERROR = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _token(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque token")
    return value


def _timestamp(value: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.isoformat()


def _outcome(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if value not in OUTCOMES:
        raise ValueError(f"{field_name} has an unsupported outcome")
    return value


@dataclass(frozen=True, slots=True)
class MemoryDualWriteReceipt:
    operation_id: str
    native_memory_id: str
    native_content_hash: str
    record_id: str
    revision_id: str
    content_hash: str
    observed_at: str
    revision_outcome: str
    native_outcomes: Mapping[str, str]
    error_codes: Mapping[str, str] = field(default_factory=dict)
    producer_kind: str = "internal"
    schema: str = SCHEMA
    receipt_id: str = field(init=False)
    authority_state: str = field(init=False)
    requires_reconciliation: bool = field(init=False)
    authority_switch_allowed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        operation_id = _token(self.operation_id, "operation_id")
        native_memory_id = _token(self.native_memory_id, "native_memory_id")
        native_content_hash = _token(self.native_content_hash, "native_content_hash")
        record_id = _token(self.record_id, "record_id")
        revision_id = _token(self.revision_id, "revision_id")
        content_hash = _token(self.content_hash, "content_hash")
        observed_at = _timestamp(self.observed_at)
        revision_outcome = _outcome(self.revision_outcome, "revision_outcome")
        producer_kind = _token(self.producer_kind, "producer_kind")
        if producer_kind not in PRODUCER_KINDS:
            raise ValueError("producer_kind is unsupported")

        supplied = dict(self.native_outcomes)
        if not set(NATIVE_BACKENDS).issubset(supplied):
            raise ValueError("native_outcomes must cover postgres, chroma and neo4j")
        if len(supplied) > MAX_NATIVE_BACKENDS:
            raise ValueError("native_outcomes exceeds the bounded backend limit")
        for backend in supplied:
            _token(backend, "native_outcomes backend")
        native = {
            backend: _outcome(supplied[backend], f"native_outcomes.{backend}")
            for backend in sorted(supplied)
        }
        errors = dict(self.error_codes)
        allowed_error_keys = {"revision", *native}
        if not set(errors).issubset(allowed_error_keys):
            raise ValueError("error_codes contains an unknown operation")
        for key, code in errors.items():
            if not _ERROR.fullmatch(str(code or "")):
                raise ValueError("error codes must be bounded classifications")
            outcome = revision_outcome if key == "revision" else native[key]
            if outcome not in {"failed", "timed_out", "cancelled"}:
                raise ValueError("error code requires a failed terminal outcome")
        failed = {
            key for key, value in {"revision": revision_outcome, **native}.items()
            if value in {"failed", "timed_out", "cancelled"}
        }
        if set(errors) != failed:
            raise ValueError("every failed terminal outcome requires one error code")

        canonical_committed = revision_outcome == "committed"
        native_committed = native["postgres"] == "committed"
        if canonical_committed and native_committed:
            authority_state = "dual_committed"
        elif canonical_committed:
            authority_state = "canonical_only"
        elif native_committed:
            authority_state = "native_only"
        else:
            authority_state = "uncommitted"
        requires_reconciliation = (
            authority_state != "dual_committed"
            or any(value not in {"committed", "skipped"} for value in native.values())
        )

        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "native_memory_id", native_memory_id)
        object.__setattr__(self, "native_content_hash", native_content_hash)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "revision_outcome", revision_outcome)
        object.__setattr__(self, "native_outcomes", MappingProxyType(native))
        object.__setattr__(self, "error_codes", MappingProxyType(dict(sorted(errors.items()))))
        object.__setattr__(self, "producer_kind", producer_kind)
        object.__setattr__(self, "authority_state", authority_state)
        object.__setattr__(self, "requires_reconciliation", requires_reconciliation)
        identity = self.identity_dict()
        object.__setattr__(self, "receipt_id", "mdwr_" + hashlib.sha256(
            _canonical(identity).encode()).hexdigest())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "native_memory_id": self.native_memory_id,
            "native_content_hash": self.native_content_hash,
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "observed_at": self.observed_at,
            "revision_outcome": self.revision_outcome,
            "native_outcomes": dict(self.native_outcomes),
            "error_codes": dict(self.error_codes),
            "producer_kind": self.producer_kind,
            "authority_state": self.authority_state,
            "requires_reconciliation": self.requires_reconciliation,
            "authority_switch_allowed": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, **self.identity_dict()}


def create_memory_dual_write_receipt(
    *, operation_id: str, native_memory_id: str, native_content_hash: str,
    revision: RecordRevision,
    observed_at: str, revision_outcome: str,
    native_outcomes: Mapping[str, str],
    error_codes: Mapping[str, str] | None = None,
    producer_kind: str = "internal",
) -> MemoryDualWriteReceipt:
    """Bind one native identity and all observed outcomes to one exact revision."""
    if not isinstance(revision, RecordRevision):
        raise TypeError("revision must be a RecordRevision")
    return MemoryDualWriteReceipt(
        operation_id=operation_id,
        native_memory_id=native_memory_id,
        native_content_hash=native_content_hash,
        record_id=revision.record_id,
        revision_id=revision.revision_id,
        content_hash=revision.content_hash,
        observed_at=observed_at,
        revision_outcome=revision_outcome,
        native_outcomes=native_outcomes,
        error_codes=error_codes or {},
        producer_kind=producer_kind,
    )
