"""Payload-free provider-operation audit receipts for portable memory."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.memory_provider import (
    MemoryAccessContext,
    MemoryAccessDenied,
    MemoryPage,
    MemoryProjection,
    MemoryProvider,
    MemoryQuery,
)


MEMORY_OPERATION_RECEIPT_SCHEMA = "vera.memory-operation-receipt/v1"
_OPERATIONS = {"apply", "read", "search"}
_OUTCOMES = {"succeeded", "denied", "not_found", "invalid", "cancelled", "failed"}
_MEMORY_ID = re.compile(r"^mem_[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^rec_[A-Za-z0-9._:-]{1,251}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")


def _canonical(value: Mapping[str, Any], field: str, limit: int = 16_384) -> str:
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{field} must be finite JSON") from exc
    if json.loads(encoded) != dict(value):
        raise ValueError(f"{field} must use JSON string object keys")
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds size limit")
    return encoded


def _safe_id(value: Any, pattern: re.Pattern[str]) -> str:
    value = value if isinstance(value, str) else ""
    return value if pattern.fullmatch(value) else ""


def _input_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode()).hexdigest()


@dataclass(frozen=True)
class MemoryOperationReceipt:
    receipt_id: str
    request_id: str
    operation: str
    outcome: str
    provider: str
    tenant_id: str
    principal_id: str
    context_hash: str
    memory_id: str = ""
    record_id: str = ""
    revision_id: str = ""
    query_id: str = ""
    result_count: int = 0
    generation: int = 0
    reason_code: str = ""
    schema: str = MEMORY_OPERATION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MEMORY_OPERATION_RECEIPT_SCHEMA:
            raise ValueError("invalid memory operation receipt schema")
        if self.operation not in _OPERATIONS or self.outcome not in _OUTCOMES:
            raise ValueError("invalid memory operation receipt semantics")
        if not self.request_id or not self.provider or not self.tenant_id \
                or not self.principal_id:
            raise ValueError("memory operation receipt identity is incomplete")
        if not self.context_hash.startswith("sha256:") or len(self.context_hash) != 71:
            raise ValueError("invalid memory operation context hash")
        if self.result_count < 0 or self.generation < 0:
            raise ValueError("memory audit counts cannot be negative")
        identity = {
            "schema": self.schema, "request_id": self.request_id,
            "operation": self.operation, "outcome": self.outcome,
            "provider": self.provider, "tenant_id": self.tenant_id,
            "principal_id": self.principal_id, "context_hash": self.context_hash,
            "memory_id": self.memory_id, "record_id": self.record_id,
            "revision_id": self.revision_id, "query_id": self.query_id,
            "result_count": self.result_count, "generation": self.generation,
            "reason_code": self.reason_code,
        }
        expected = "mop_" + hashlib.sha256(
            _canonical(identity, "memory audit identity").encode()).hexdigest()
        if self.receipt_id != expected:
            raise ValueError("memory operation receipt checksum mismatch")

    @classmethod
    def create(cls, *, request_id: str, operation: str, outcome: str,
               provider: str, access: MemoryAccessContext,
               context: Mapping[str, Any], memory_id: str = "",
               record_id: str = "", revision_id: str = "", query_id: str = "",
               result_count: int = 0, generation: int = 0,
               reason_code: str = "") -> "MemoryOperationReceipt":
        if not access.request_id or request_id != access.request_id:
            raise ValueError("audit receipt requires matching access request_id")
        if operation not in _OPERATIONS:
            raise ValueError("invalid memory audit operation")
        if outcome not in _OUTCOMES:
            raise ValueError("invalid memory audit outcome")
        provider = str(provider or "").strip()
        if not provider or len(provider) > 256:
            raise ValueError("invalid memory audit provider")
        reason_code = str(reason_code or "").strip()
        if len(reason_code) > 128:
            raise ValueError("memory audit reason_code exceeds size limit")
        result_count = int(result_count)
        generation = int(generation)
        if result_count < 0 or generation < 0:
            raise ValueError("memory audit counts cannot be negative")
        context_json = _canonical(context, "memory audit context")
        context_hash = "sha256:" + hashlib.sha256(context_json.encode()).hexdigest()
        identity = {
            "schema": MEMORY_OPERATION_RECEIPT_SCHEMA,
            "request_id": request_id, "operation": operation,
            "outcome": outcome, "provider": provider,
            "tenant_id": access.tenant_id, "principal_id": access.principal_id,
            "context_hash": context_hash, "memory_id": str(memory_id or ""),
            "record_id": str(record_id or ""),
            "revision_id": str(revision_id or ""), "query_id": str(query_id or ""),
            "result_count": result_count, "generation": generation,
            "reason_code": reason_code,
        }
        receipt_id = "mop_" + hashlib.sha256(
            _canonical(identity, "memory audit identity").encode()).hexdigest()
        return cls(receipt_id=receipt_id, request_id=request_id,
                   operation=operation, outcome=outcome, provider=provider,
                   tenant_id=access.tenant_id, principal_id=access.principal_id,
                   context_hash=context_hash, memory_id=identity["memory_id"],
                   record_id=identity["record_id"],
                   revision_id=identity["revision_id"], query_id=identity["query_id"],
                   result_count=result_count, generation=generation,
                   reason_code=reason_code)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class MemoryAuditSink(Protocol):
    def append(self, receipt: MemoryOperationReceipt) -> None: ...


class FrozenMemoryAuditSink:
    """Bounded, idempotent reference sink; not a durable production ledger."""

    def __init__(self, max_receipts: int = 10_000):
        self._max_receipts = max(1, int(max_receipts))
        self._receipts: dict[str, MemoryOperationReceipt] = {}

    def append(self, receipt: MemoryOperationReceipt) -> None:
        if not isinstance(receipt, MemoryOperationReceipt):
            raise TypeError("receipt must be MemoryOperationReceipt")
        current = self._receipts.get(receipt.receipt_id)
        if current is not None:
            if current != receipt:
                raise ValueError("memory audit receipt identity collision")
            return
        if len(self._receipts) >= self._max_receipts:
            raise ValueError("memory audit sink limit reached")
        self._receipts[receipt.receipt_id] = receipt

    def receipts(self) -> tuple[MemoryOperationReceipt, ...]:
        return tuple(self._receipts.values())


def _outcome(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, MemoryAccessDenied):
        return "denied", "access_denied"
    if isinstance(exc, KeyError):
        return "not_found", "not_found"
    if isinstance(exc, QueryCancelled):
        return "cancelled", "cancelled"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid", "invalid_request"
    return "failed", "provider_error"


class AuditedMemoryProvider:
    """Provider-neutral wrapper emitting one receipt per attempted operation.

    Sink failure is fail-closed after a successful provider call.  When the
    provider itself fails, its original exception is retained and annotated if
    the sink also fails.
    """

    def __init__(self, provider: MemoryProvider, sink: MemoryAuditSink):
        self._provider = provider
        self._sink = sink
        self.name = str(getattr(provider, "name", "") or "unknown-memory-provider")

    @staticmethod
    def _require_request(access: MemoryAccessContext) -> None:
        if not access.request_id:
            raise ValueError("audited memory operation requires access request_id")

    def _emit(self, *, operation: str, outcome: str, access: MemoryAccessContext,
              context: Mapping[str, Any], reason_code: str = "", **values) -> None:
        self._sink.append(MemoryOperationReceipt.create(
            request_id=access.request_id, operation=operation, outcome=outcome,
            provider=self.name, access=access, context=context,
            reason_code=reason_code, **values))

    def _failure(self, operation: str, access: MemoryAccessContext,
                 context: Mapping[str, Any], exc: Exception, **values) -> None:
        outcome, reason = _outcome(exc)
        try:
            self._emit(operation=operation, outcome=outcome, access=access,
                       context=context, reason_code=reason, **values)
        except Exception as audit_exc:
            exc.add_note(f"memory audit sink also failed: {type(audit_exc).__name__}")

    def apply(self, projection: MemoryProjection,
              access: MemoryAccessContext) -> MemoryProjection:
        self._require_request(access)
        context = {"memory_id": _safe_id(
                       getattr(projection, "memory_id", ""), _MEMORY_ID),
                   "record_id": _safe_id(
                       getattr(projection, "record_id", ""), _RECORD_ID),
                   "revision_id": _safe_id(
                       getattr(projection, "revision_id", ""), _REVISION_ID),
                   "tenant_id": getattr(projection, "tenant_id", ""),
                   "tombstone": getattr(projection, "tombstone", False)}
        try:
            result = self._provider.apply(projection, access)
        except Exception as exc:
            self._failure("apply", access, context, exc,
                          memory_id=context["memory_id"],
                          record_id=context["record_id"],
                          revision_id=context["revision_id"])
            raise
        self._emit(operation="apply", outcome="succeeded", access=access,
                   context=context, memory_id=result.memory_id,
                   record_id=result.record_id, revision_id=result.revision_id)
        return result

    def get(self, memory_id: str, access: MemoryAccessContext,
            *, include_text: bool = True) -> dict:
        self._require_request(access)
        safe_memory_id = _safe_id(memory_id, _MEMORY_ID)
        context = {"memory_id": safe_memory_id,
                   "input_hash": _input_hash(memory_id),
                   "include_text": bool(include_text)}
        try:
            result = self._provider.get(memory_id, access, include_text=include_text)
        except Exception as exc:
            self._failure("read", access, context, exc, memory_id=safe_memory_id)
            raise
        self._emit(operation="read", outcome="succeeded", access=access,
                   context=context, memory_id=str(result.get("memory_id", memory_id)),
                   record_id=str(result.get("record_id", "")),
                   revision_id=str(result.get("revision_id", "")))
        return result

    def search(self, query: MemoryQuery, access: MemoryAccessContext, *,
               cancellation: CancellationSignal | None = None) -> MemoryPage:
        self._require_request(access)
        context = {"query_id": getattr(query, "query_id", ""),
                   "tenant_id": getattr(query, "tenant_id", ""),
                   "namespace": getattr(query, "namespace", ""),
                   "session_id": getattr(query, "session_id", ""),
                   "record_type": getattr(query, "record_type", ""),
                   "tags": list(getattr(query, "tags", ())),
                   "include_tombstones": getattr(query, "include_tombstones", False),
                   "include_text": getattr(query, "include_text", True),
                   "limit": getattr(query, "limit", 0),
                   "has_cursor": bool(getattr(query, "cursor", ""))}
        try:
            result = self._provider.search(query, access, cancellation=cancellation)
        except Exception as exc:
            self._failure("search", access, context, exc,
                          query_id=str(context["query_id"]))
            raise
        self._emit(operation="search", outcome="succeeded", access=access,
                   context=context, query_id=result.query_id,
                   result_count=len(result.hits), generation=result.generation)
        return result
