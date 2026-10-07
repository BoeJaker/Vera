"""Restart-safe executor for payload-free Memory dual-write reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping

from .memory_dual_write_store import MemoryDualWriteReceiptStore


Repair = Callable[[str, Mapping[str, object], str], Awaitable[str]]
_ERROR = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class MemoryReconciliationResult:
    attempt_id: str
    operation_id: str
    targets: tuple[str, ...]
    outcomes: Mapping[str, str]
    error_codes: Mapping[str, str]
    converged: bool
    authority_switch_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", MappingProxyType(dict(self.outcomes)))
        object.__setattr__(self, "error_codes", MappingProxyType(dict(self.error_codes)))


def _error_code(exc: BaseException) -> str:
    name = _ERROR.sub("_", type(exc).__name__.lower()).strip("_")
    return (name or "repair_failed")[:128]


async def reconcile_memory_dual_write(
    store: MemoryDualWriteReceiptStore, *, operation_id: str,
    attempt_id: str, started_at: str, completed_at: str, repair: Repair,
    timeout_s: float | None = None,
) -> MemoryReconciliationResult:
    """Resume or execute one exact, idempotent reconciliation attempt.

    ``repair`` receives a stable target-specific idempotency key. Implementors
    must make that key idempotent because a process can stop after the external
    effect but before the completion record commits.
    """
    if not isinstance(store, MemoryDualWriteReceiptStore):
        raise TypeError("store must be a MemoryDualWriteReceiptStore")
    if not callable(repair):
        raise TypeError("repair must be callable")
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    prior = store.attempt(attempt_id)
    if prior and prior["state"] == "completed":
        status = store.reconciliation_status(prior["operation_id"])
        return MemoryReconciliationResult(
            attempt_id=attempt_id, operation_id=prior["operation_id"],
            targets=tuple(prior["targets"]), outcomes=prior["outcomes"],
            error_codes=prior["error_codes"], converged=status["converged"])

    status = store.reconciliation_status(operation_id)
    targets = list(status["unresolved_targets"])
    attempt = store.start_reconciliation(
        operation_id=operation_id, attempt_id=attempt_id,
        targets=targets, started_at=started_at)
    receipt = store.get(operation_id)
    outcomes: dict[str, str] = {}
    errors: dict[str, str] = {}
    for index, target in enumerate(attempt["targets"]):
        key = f"{attempt_id}:{target}"
        try:
            call = repair(target, receipt, key)
            outcome = (await call if timeout_s is None else
                       await asyncio.wait_for(call, timeout=timeout_s))
            if outcome != "committed":
                outcomes[target] = "failed"
                errors[target] = "repair_not_committed"
            else:
                outcomes[target] = "committed"
        except asyncio.CancelledError:
            outcomes[target] = "cancelled"
            errors[target] = "repair_cancelled"
            for remaining in attempt["targets"][index + 1:]:
                outcomes[remaining] = "skipped"
            try:
                store.complete_reconciliation(
                    attempt_id=attempt_id, outcomes=outcomes,
                    error_codes=errors, completed_at=completed_at)
            except Exception:
                # Cancellation ownership is never transferred to evidence I/O.
                # The durable started attempt remains discoverable for restart.
                pass
            raise
        except TimeoutError:
            outcomes[target] = "timed_out"
            errors[target] = "repair_timeout"
        except Exception as exc:
            outcomes[target] = "failed"
            errors[target] = _error_code(exc)
    completed = store.complete_reconciliation(
        attempt_id=attempt_id, outcomes=outcomes,
        error_codes=errors, completed_at=completed_at)
    final = store.reconciliation_status(operation_id)
    return MemoryReconciliationResult(
        attempt_id=attempt_id, operation_id=operation_id,
        targets=tuple(completed["targets"]), outcomes=completed["outcomes"],
        error_codes=completed["error_codes"], converged=final["converged"])
