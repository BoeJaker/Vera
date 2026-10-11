"""Default-off concrete binding for manual Memory dual-write repair."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .memory_dual_write_reconciliation import reconcile_memory_dual_write
from .memory_dual_write_readiness import assess_memory_authority_readiness
from .memory_dual_write_repairs import MemoryDualWriteRepairAdapter, NATIVE_TARGETS
from .memory_dual_write_runtime import (
    default_memory_dual_write_receipt_path,
    default_memory_revision_path,
    native_memory_content_hash,
)
from .memory_dual_write_store import MemoryDualWriteReceiptStore
from .record_revision import RecordRevision, record_revision_from_dict
from .revision_path import RevisionPath
from .revision_policy import RevisionPolicy
from .revision_store import RevisionStore


ENABLED_ENV = "VERA_MEMORY_RECONCILIATION_ENABLED"
REVISION_PATH_ENV = "FABRIC_REVISION_SQLITE"
RECEIPT_PATH_ENV = "VERA_MEMORY_DUAL_WRITE_RECEIPTS"
POLICY_ENV = "FABRIC_REVISION_POLICY"

NativeLoader = Callable[[str, str], Awaitable[Mapping[str, Any] | None]]
NativeWriter = Callable[[str, Mapping[str, Any]], Awaitable[bool]]


def _enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(ENABLED_ENV, "0")).strip() == "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_memory_authority_readiness(
    *, limit: int = 100, min_receipts: int = 25,
    max_evidence_age_seconds: int = 86_400,
    environ: Mapping[str, str] | None = None, assessed_at: str = "",
    expected_source_commit: str = "",
) -> dict[str, Any]:
    """Read bounded journal evidence; never repair or switch authority."""
    env = os.environ if environ is None else environ
    receipt_file = Path(str(
        env.get(RECEIPT_PATH_ENV) or default_memory_dual_write_receipt_path()))
    if receipt_file.exists():
        store = MemoryDualWriteReceiptStore(receipt_file)
        evidence = store.readiness_evidence(limit=limit)
        rollback = store.latest_rollback_rehearsal()
    else:
        evidence = {
            "total_receipts": 0, "returned_receipts": 0,
            "truncated": False, "unresolved_operations": 0,
            "pending_attempts": 0, "failed_attempts": 0,
            "latest_observed_at": "", "retains_payload": False,
            "executes": False, "authority_switch_allowed": False,
        }
        rollback = None
    result = assess_memory_authority_readiness(
        evidence, assessed_at=assessed_at or _now(),
        rollback_receipt=rollback,
        expected_source_commit=expected_source_commit,
        min_receipts=min_receipts,
        max_evidence_age_seconds=max_evidence_age_seconds)
    return result.to_dict()


async def run_manual_memory_reconciliation(
    *, operation_id: str, attempt_id: str, actor: str,
    native_loader: NativeLoader, native_writer: NativeWriter,
    timeout_s: float = 30.0, environ: Mapping[str, str] | None = None,
    started_at: str = "", completed_at: str = "",
) -> dict[str, Any]:
    """Repair one journalled operation through exact existing authorities.

    The gate is checked before paths or policy are constructed. This function
    does not enumerate work, schedule retries, or grant read/write authority.
    """
    env = os.environ if environ is None else environ
    if not _enabled(env):
        raise PermissionError("memory reconciliation is disabled")
    if not callable(native_loader) or not callable(native_writer):
        raise TypeError("native loader and writer must be callable")
    timeout_s = float(timeout_s)
    if timeout_s <= 0 or timeout_s > 300:
        raise ValueError("timeout_s must be between 0 and 300")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("actor is required")

    revision_file = str(
        env.get(REVISION_PATH_ENV) or default_memory_revision_path())
    receipt_file = str(
        env.get(RECEIPT_PATH_ENV) or default_memory_dual_write_receipt_path())
    policy = RevisionPolicy.from_json(str(env.get(POLICY_ENV, "")))
    revision_path = RevisionPath(RevisionStore(revision_file), policy.allows)
    receipt_store = MemoryDualWriteReceiptStore(receipt_file)

    async def load_native(native_id: str) -> Mapping[str, Any]:
        value = await native_loader("postgres", native_id)
        if value is None:
            raise KeyError("authoritative native memory is unavailable")
        return value

    async def commit_revision(
        revision: RecordRevision, key: str,
    ) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            revision_path.put, revision, actor=actor, projections=("memory",))
        if result.get("authority_commit") != "committed":
            raise ValueError("revision authority did not commit")
        return {
            "outcome": "committed", "target": "revision",
            "native_memory_id": revision.logical_key,
            "record_id": revision.record_id,
            "revision_id": revision.revision_id,
            "idempotency_key": key,
        }

    async def load_revision(record_id: str, revision_id: str) -> RecordRevision:
        envelope = await asyncio.to_thread(
            revision_path.get, record_id, actor=actor, revision_id=revision_id)
        if envelope is None:
            raise KeyError("canonical revision is unavailable")
        return record_revision_from_dict(envelope)

    writers = {}
    for target in NATIVE_TARGETS:
        async def bound(snapshot, key, target=target):
            committed = await native_writer(target, snapshot)
            if committed is not True:
                raise ValueError("native backend did not commit")
            persisted = await native_loader(target, str(snapshot["id"]))
            if (persisted is None
                    or str(persisted.get("id") or "") != str(snapshot["id"])
                    or native_memory_content_hash(persisted)
                    != native_memory_content_hash(snapshot)):
                raise ValueError("native backend did not verify committed content")
            evidence = receipt_store.get(operation_id)
            return {
                "outcome": "committed", "target": target,
                "native_memory_id": str(snapshot["id"]),
                "record_id": str(evidence["record_id"]),
                "revision_id": str(evidence["revision_id"]),
                "idempotency_key": key,
            }
        writers[target] = bound

    repair = MemoryDualWriteRepairAdapter(
        native_snapshot_loader=load_native,
        revision_committer=commit_revision,
        revision_loader=load_revision,
        backend_writers=writers,
    )
    result = await reconcile_memory_dual_write(
        receipt_store, operation_id=operation_id, attempt_id=attempt_id,
        started_at=started_at or _now(), completed_at=completed_at or _now,
        repair=repair, timeout_s=timeout_s)
    return {
        "attempt_id": result.attempt_id,
        "operation_id": result.operation_id,
        "targets": list(result.targets),
        "outcomes": dict(result.outcomes),
        "error_codes": dict(result.error_codes),
        "converged": result.converged,
        "authority_switch_allowed": False,
    }
