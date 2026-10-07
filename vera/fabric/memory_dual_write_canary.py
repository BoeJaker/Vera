"""Injected, default-off Memory → RecordRevision shadow-write coordinator.

Native Memory remains authoritative.  When enabled, the exact native result is
returned even if the canonical shadow write or evidence journal fails.  Every
successfully journalled attempt remains non-authorizing and reconciliation is
explicit.  No production runtime imports this module yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .memory_dual_write_receipt import (
    NATIVE_BACKENDS,
    MemoryDualWriteReceipt,
    create_memory_dual_write_receipt,
)
from .record_revision import RecordRevision


NativeWriter = Callable[[], Awaitable[Mapping[str, bool | str]]]
RevisionWriter = Callable[[RecordRevision], Awaitable[Any]]


class ReceiptSink(Protocol):
    def append(self, receipt: MemoryDualWriteReceipt) -> Any: ...


@dataclass(frozen=True, slots=True)
class MemoryDualWriteCanaryResult:
    native_result: Mapping[str, bool | str]
    enabled: bool
    revision_attempted: bool
    receipt: MemoryDualWriteReceipt | None
    evidence_persisted: bool
    evidence_error_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "native_result", MappingProxyType(dict(self.native_result)))


def _classify_native(result: Mapping[str, bool | str]) -> dict[str, str]:
    if not isinstance(result, Mapping):
        raise TypeError("native writer must return a backend outcome mapping")
    unknown = set(result) - set(NATIVE_BACKENDS)
    if unknown:
        raise ValueError("native writer returned an unknown backend")
    classified: dict[str, str] = {}
    for backend in NATIVE_BACKENDS:
        value = result.get(backend, "skipped")
        if value is True:
            value = "committed"
        elif value is False:
            value = "failed"
        if value not in {"committed", "failed", "timed_out", "cancelled", "skipped"}:
            raise ValueError("native writer returned an unsupported outcome")
        classified[backend] = str(value)
    return classified


def _native_error(code: str, outcome: str) -> tuple[dict[str, str], dict[str, str]]:
    native = {backend: outcome for backend in NATIVE_BACKENDS}
    errors = {backend: code for backend in NATIVE_BACKENDS}
    return native, errors


def _persist(sink: ReceiptSink, receipt: MemoryDualWriteReceipt) -> tuple[bool, str]:
    try:
        sink.append(receipt)
        return True, ""
    except Exception:
        # The legacy write already happened.  Evidence failure is explicit but
        # cannot turn that successful native write into a caller-visible error.
        return False, "receipt_store_failed"


async def execute_memory_dual_write_canary(
    *, enabled: bool = False, operation_id: str, native_memory_id: str,
    native_content_hash: str, revision: RecordRevision, observed_at: str,
    native_write: NativeWriter, revision_write: RevisionWriter,
    receipt_sink: ReceiptSink, native_timeout_s: float = 30.0,
    revision_timeout_s: float = 10.0,
) -> MemoryDualWriteCanaryResult:
    """Run the native write once and optionally observe a canonical shadow write.

    Revision failure is isolated from the native result.  Cancellation always
    propagates after best-effort durable evidence for an enabled canary.
    """
    if not isinstance(revision, RecordRevision):
        raise TypeError("revision must be a RecordRevision")
    if native_timeout_s <= 0 or revision_timeout_s <= 0:
        raise ValueError("timeouts must be positive")

    raw_native: Mapping[str, bool | str] = {}
    try:
        raw_native = await asyncio.wait_for(native_write(), timeout=native_timeout_s)
        native_outcomes = _classify_native(raw_native)
    except asyncio.CancelledError:
        if enabled:
            native_outcomes, errors = _native_error("native_cancelled", "cancelled")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors)
            _persist(receipt_sink, receipt)
        raise
    except TimeoutError:
        if enabled:
            native_outcomes, errors = _native_error("native_timeout", "timed_out")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors)
            _persist(receipt_sink, receipt)
        raise
    except Exception:
        if enabled:
            native_outcomes, errors = _native_error("native_write_failed", "failed")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors)
            _persist(receipt_sink, receipt)
        raise

    if not enabled:
        return MemoryDualWriteCanaryResult(
            native_result=raw_native, enabled=False, revision_attempted=False,
            receipt=None, evidence_persisted=False, evidence_error_code="")

    errors = {
        backend: "native_write_failed"
        for backend, outcome in native_outcomes.items()
        if outcome == "failed"
    }
    errors.update({
        backend: f"native_{outcome}"
        for backend, outcome in native_outcomes.items()
        if outcome in {"timed_out", "cancelled"}
    })
    revision_outcome = "committed"
    try:
        result = await asyncio.wait_for(revision_write(revision), timeout=revision_timeout_s)
        if not isinstance(result, Mapping) or result.get("authority_commit") != "committed":
            revision_outcome = "failed"
            errors["revision"] = "revision_not_committed"
    except asyncio.CancelledError:
        revision_outcome = "cancelled"
        errors["revision"] = "revision_cancelled"
        receipt = create_memory_dual_write_receipt(
            operation_id=operation_id, native_memory_id=native_memory_id,
            native_content_hash=native_content_hash, revision=revision,
            observed_at=observed_at, revision_outcome=revision_outcome,
            native_outcomes=native_outcomes, error_codes=errors)
        _persist(receipt_sink, receipt)
        raise
    except TimeoutError:
        revision_outcome = "timed_out"
        errors["revision"] = "revision_timeout"
    except Exception:
        revision_outcome = "failed"
        errors["revision"] = "revision_write_failed"

    receipt = create_memory_dual_write_receipt(
        operation_id=operation_id, native_memory_id=native_memory_id,
        native_content_hash=native_content_hash, revision=revision,
        observed_at=observed_at, revision_outcome=revision_outcome,
        native_outcomes=native_outcomes, error_codes=errors)
    persisted, evidence_error = _persist(receipt_sink, receipt)
    return MemoryDualWriteCanaryResult(
        native_result=raw_native, enabled=True, revision_attempted=True,
        receipt=receipt, evidence_persisted=persisted,
        evidence_error_code=evidence_error)
