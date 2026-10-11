"""Deterministic producer for payload-free Memory rollback evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .memory_dual_write_readiness import MemoryRollbackRehearsalReceipt
from .memory_dual_write_runtime import (
    default_memory_dual_write_receipt_path,
    store_public_memory_with_revision_canary,
)
from .memory_dual_write_store import MemoryDualWriteReceiptStore, MemoryReceiptConflict


CanaryRunner = Callable[..., Awaitable[Any]]
ReconciliationRunner = Callable[..., Awaitable[Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def run_memory_rollback_rehearsal(
    *, rehearsal_id: str, source_commit: str,
    environ: Mapping[str, str] | None = None, completed_at: str = "",
) -> dict[str, Any]:
    """Bind the deterministic producer to the configured evidence journal."""
    env = os.environ if environ is None else environ
    path = Path(str(env.get("VERA_MEMORY_DUAL_WRITE_RECEIPTS") or
                    default_memory_dual_write_receipt_path()))
    store = MemoryDualWriteReceiptStore(path)
    return await produce_memory_rollback_rehearsal(
        store=store, rehearsal_id=rehearsal_id,
        source_commit=source_commit, completed_at=completed_at)


async def produce_memory_rollback_rehearsal(
    *, store: MemoryDualWriteReceiptStore, rehearsal_id: str,
    source_commit: str, completed_at: str = "",
    canary_runner: CanaryRunner = store_public_memory_with_revision_canary,
    reconciliation_runner: ReconciliationRunner | None = None,
) -> dict[str, Any]:
    """Exercise real disabled gates using sentinels, then persist the proof.

    No native backend callback is supplied.  The only callback that may run is
    the local sentinel standing in for the still-authoritative native write.
    Deliberately invalid policy, time and state paths prove that each disabled
    gate returns before constructing canonical or reconciliation state.
    """
    if not isinstance(store, MemoryDualWriteReceiptStore):
        raise TypeError("store must be a MemoryDualWriteReceiptStore")
    if not callable(canary_runner):
        raise TypeError("canary_runner must be callable")
    if reconciliation_runner is None:
        from .memory_dual_write_repair_runtime import run_manual_memory_reconciliation
        reconciliation_runner = run_manual_memory_reconciliation
    if not callable(reconciliation_runner):
        raise TypeError("reconciliation_runner must be callable")

    # Stable operation identity: an exact replay performs no probe or write.
    existing = store.rollback_rehearsal(rehearsal_id)
    if existing is not None:
        if existing.source_commit != str(source_commit or "").strip():
            raise MemoryReceiptConflict("rollback rehearsal source commit differs")
        return {"created": False, "receipt": existing.to_dict()}

    sentinel = {"native": "available"}
    native_calls = 0

    async def native_write():
        nonlocal native_calls
        native_calls += 1
        return sentinel

    impossible = "rollback-rehearsal://must-not-open"
    canary_result = await canary_runner(
        {"deliberately": "invalid"}, native_write=native_write,
        actor="", observed_at="not-a-time",
        environ={
            "VERA_MEMORY_REVISION_CANARY": "0",
            "FABRIC_REVISION_SQLITE": impossible,
            "VERA_MEMORY_DUAL_WRITE_RECEIPTS": impossible,
            "FABRIC_REVISION_POLICY": "not-json",
        })
    canary_disabled = canary_result is sentinel and native_calls == 1

    reconciliation_calls: list[tuple[str, str]] = []

    async def load_native(backend: str, native_id: str) -> Mapping[str, Any] | None:
        reconciliation_calls.append(("load", backend))
        return None

    async def write_native(backend: str, snapshot: Mapping[str, Any]) -> bool:
        reconciliation_calls.append(("write", backend))
        return False

    denied = False
    try:
        await reconciliation_runner(
            operation_id="rollback-rehearsal", attempt_id="disabled-probe",
            actor="", native_loader=load_native, native_writer=write_native,
            environ={
                "VERA_MEMORY_RECONCILIATION_ENABLED": "0",
                "FABRIC_REVISION_SQLITE": impossible,
                "VERA_MEMORY_DUAL_WRITE_RECEIPTS": impossible,
                "FABRIC_REVISION_POLICY": "not-json",
            })
    except PermissionError:
        denied = True
    reconciliation_disabled = denied and not reconciliation_calls

    receipt = MemoryRollbackRehearsalReceipt(
        rehearsal_id=rehearsal_id, source_commit=source_commit,
        completed_at=completed_at or _now(),
        canary_disabled=canary_disabled,
        native_write_available=canary_result is sentinel and native_calls == 1,
        revision_write_attempted=not canary_disabled,
        reconciliation_disabled=reconciliation_disabled,
    )
    if not receipt.passed:
        raise RuntimeError("rollback rehearsal did not prove every disabled-path invariant")
    try:
        return store.append_rollback_rehearsal(receipt)
    except MemoryReceiptConflict:
        # A concurrent caller can finish the same harmless local probe first.
        # Accept only its passing receipt for this exact operation and source;
        # changed intent remains a conflict.
        winner = store.rollback_rehearsal(rehearsal_id)
        if (winner is not None and winner.passed
                and winner.source_commit == receipt.source_commit):
            return {"created": False, "receipt": winner.to_dict()}
        raise
