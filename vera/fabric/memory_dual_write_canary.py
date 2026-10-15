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
RevisionFactory = Callable[[], RecordRevision]


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
    if len(result) > 32:
        raise ValueError("native writer exceeded the bounded backend limit")
    classified: dict[str, str] = {}
    for backend in sorted({*NATIVE_BACKENDS, *result}):
        if (not isinstance(backend, str) or not backend
                or len(backend) > 256
                or any(not (char.isalnum() or char in "_.:-") for char in backend)):
            raise ValueError("native writer returned an invalid backend name")
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
    receipt_sink: ReceiptSink, native_timeout_s: float | None = 30.0,
    revision_timeout_s: float | None = 10.0,
    revision_factory: RevisionFactory | None = None,
    producer_kind: str = "internal",
) -> MemoryDualWriteCanaryResult:
    """Run the native write once and optionally observe a canonical shadow write.

    Revision failure is isolated from the native result.  Cancellation always
    propagates after best-effort durable evidence for an enabled canary.
    """
    if not isinstance(revision, RecordRevision):
        raise TypeError("revision must be a RecordRevision")
    if ((native_timeout_s is not None and native_timeout_s <= 0)
            or (revision_timeout_s is not None and revision_timeout_s <= 0)):
        raise ValueError("timeouts must be positive")

    raw_native: Mapping[str, bool | str] = {}
    try:
        raw_native = (await native_write() if native_timeout_s is None else
                      await asyncio.wait_for(native_write(), timeout=native_timeout_s))
    except asyncio.CancelledError:
        if enabled:
            native_outcomes, errors = _native_error("native_cancelled", "cancelled")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors,
                producer_kind=producer_kind)
            _persist(receipt_sink, receipt)
        raise
    except TimeoutError:
        if enabled:
            native_outcomes, errors = _native_error("native_timeout", "timed_out")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors,
                producer_kind=producer_kind)
            _persist(receipt_sink, receipt)
        raise
    except Exception:
        if enabled:
            native_outcomes, errors = _native_error("native_write_failed", "failed")
            receipt = create_memory_dual_write_receipt(
                operation_id=operation_id, native_memory_id=native_memory_id,
                native_content_hash=native_content_hash, revision=revision,
                observed_at=observed_at, revision_outcome="skipped",
                native_outcomes=native_outcomes, error_codes=errors,
                producer_kind=producer_kind)
            _persist(receipt_sink, receipt)
        raise

    try:
        native_outcomes = _classify_native(raw_native)
    except Exception:
        # Classification is shadow evidence, not part of the authoritative
        # backend contract.  Once the native call returned successfully its
        # exact result must remain caller-visible even if evidence is malformed.
        return MemoryDualWriteCanaryResult(
            native_result=raw_native, enabled=enabled, revision_attempted=False,
            receipt=None, evidence_persisted=False,
            evidence_error_code="native_outcome_classification_failed")

    if not enabled:
        return MemoryDualWriteCanaryResult(
            native_result=raw_native, enabled=False, revision_attempted=False,
            receipt=None, evidence_persisted=False, evidence_error_code="")

    revision_factory_error = False
    if revision_factory is not None:
        try:
            refreshed = revision_factory()
            if not isinstance(refreshed, RecordRevision):
                raise TypeError("revision_factory must return a RecordRevision")
            revision = refreshed
        except Exception:
            # Native authority has already committed.  A shadow snapshot
            # refresh must therefore degrade to explicit evidence, never turn
            # the caller's successful write into an error.
            revision_factory_error = True

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
    revision_outcome = "failed" if revision_factory_error else "committed"
    if revision_factory_error:
        errors["revision"] = "revision_snapshot_failed"
    try:
        if not revision_factory_error:
            result = (await revision_write(revision) if revision_timeout_s is None else
                      await asyncio.wait_for(
                          revision_write(revision), timeout=revision_timeout_s))
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
            native_outcomes=native_outcomes, error_codes=errors,
            producer_kind=producer_kind)
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
        native_outcomes=native_outcomes, error_codes=errors,
        producer_kind=producer_kind)
    persisted, evidence_error = _persist(receipt_sink, receipt)
    return MemoryDualWriteCanaryResult(
        native_result=raw_native, enabled=True, revision_attempted=True,
        receipt=receipt, evidence_persisted=persisted,
        evidence_error_code=evidence_error)
