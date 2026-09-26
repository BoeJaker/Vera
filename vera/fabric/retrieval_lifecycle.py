"""Explicit lifecycle, recovery and teardown evidence for retrieval adapters.

The ordinary comparison executor intentionally keeps lifecycle metrics separate
from query quality, but historically collapsed every lifecycle failure to null
metrics.  This coordinator preserves the reason: unavailable, failed,
cancelled, timed out or unsupported.  Recovery and teardown are opt-in and run
sequentially; neither authorises activation nor selects a fallback provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import time
from typing import Any, Mapping, Sequence

from .dataset_provider import CancellationSignal, DatasetSnapshot, QueryCancelled
from .retrieval_comparison import RetrievalLifecycleMetrics, RetrievalProviderProfile
from .retrieval_execution import RetrievalProviderFailure, RetrievalProviderUnavailable


MAX_LIFECYCLE_PROVIDERS = 16
MIN_TIMEOUT_SECONDS = 0.01
MAX_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class LifecyclePhaseEvidence:
    status: str
    elapsed_ms: int | None = None
    error_code: str = ""
    metrics: RetrievalLifecycleMetrics | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "completed", "unavailable", "failed", "cancelled", "timed_out",
            "unsupported", "not_requested",
        }:
            raise ValueError("unsupported lifecycle phase status")
        if self.elapsed_ms is not None and (
                isinstance(self.elapsed_ms, bool) or
                not isinstance(self.elapsed_ms, int) or self.elapsed_ms < 0):
            raise ValueError("elapsed_ms must be a non-negative integer")
        if self.status == "completed":
            if self.error_code or not isinstance(self.metrics, RetrievalLifecycleMetrics):
                raise ValueError("completed phases require metrics and no error")
        elif self.metrics is not None:
            raise ValueError("unfinished phases cannot claim lifecycle metrics")
        if self.status in {"unavailable", "failed"} and not self.error_code:
            raise ValueError("unavailable and failed phases require an error code")
        if self.status not in {"unavailable", "failed"} and self.error_code:
            raise ValueError("only unavailable or failed phases may carry an error code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "metrics": self.metrics.to_dict() if self.metrics else None,
        }


@dataclass(frozen=True)
class ProviderLifecycleEvidence:
    snapshot_id: str
    profile: RetrievalProviderProfile
    baseline: LifecyclePhaseEvidence
    recovery: LifecyclePhaseEvidence
    teardown: LifecyclePhaseEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "profile": self.profile.to_dict(),
            "baseline": self.baseline.to_dict(),
            "recovery": self.recovery.to_dict(),
            "teardown": self.teardown.to_dict(),
        }


async def _lifecycle_phase(adapter: Any, snapshot: DatasetSnapshot,
                           signal: CancellationSignal,
                           timeout_seconds: float) -> LifecyclePhaseEvidence:
    started = time.monotonic()
    try:
        metrics = await asyncio.wait_for(
            adapter.lifecycle(snapshot, signal), timeout=timeout_seconds)
        if not isinstance(metrics, RetrievalLifecycleMetrics):
            raise RetrievalProviderFailure("invalid_lifecycle_receipt")
        return LifecyclePhaseEvidence(
            "completed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            metrics=metrics)
    except asyncio.TimeoutError:
        return LifecyclePhaseEvidence(
            "timed_out", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except QueryCancelled:
        return LifecyclePhaseEvidence(
            "cancelled", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except RetrievalProviderUnavailable as exc:
        return LifecyclePhaseEvidence(
            "unavailable", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except RetrievalProviderFailure as exc:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except Exception:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code="provider_error")


async def _invoke_optional(method: Any, *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    return await asyncio.to_thread(method, *args, **kwargs)


async def _recovery_phase(adapter: Any, snapshot: DatasetSnapshot,
                          signal: CancellationSignal,
                          timeout_seconds: float) -> LifecyclePhaseEvidence:
    recover = getattr(adapter, "recover", None)
    if not callable(recover):
        return LifecyclePhaseEvidence("unsupported")
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _invoke_optional(recover, snapshot, signal), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return LifecyclePhaseEvidence(
            "timed_out", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except QueryCancelled:
        return LifecyclePhaseEvidence(
            "cancelled", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except RetrievalProviderUnavailable as exc:
        return LifecyclePhaseEvidence(
            "unavailable", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except RetrievalProviderFailure as exc:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except Exception:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code="provider_error")
    # Recovery is proven only by a fresh lifecycle observation.
    observed = await _lifecycle_phase(adapter, snapshot, signal, timeout_seconds)
    if observed.elapsed_ms is None:
        return observed
    return LifecyclePhaseEvidence(
        observed.status,
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        error_code=observed.error_code,
        metrics=observed.metrics,
    )


async def _teardown_phase(adapter: Any, snapshot: DatasetSnapshot,
                          signal: CancellationSignal,
                          timeout_seconds: float) -> LifecyclePhaseEvidence:
    teardown = getattr(adapter, "teardown", None)
    if not callable(teardown):
        return LifecyclePhaseEvidence("unsupported")
    started = time.monotonic()
    try:
        receipt = await asyncio.wait_for(
            _invoke_optional(teardown, cancellation=signal), timeout=timeout_seconds)
        if not isinstance(receipt, Mapping):
            raise RetrievalProviderFailure("invalid_teardown_receipt")
        if receipt.get("snapshot_id") != snapshot.snapshot_id:
            raise RetrievalProviderFailure("teardown_snapshot_mismatch")
        if receipt.get("active") is not False:
            raise RetrievalProviderFailure("teardown_not_confirmed")
        deletion_ms = receipt.get("deletion_ms")
        if isinstance(deletion_ms, bool) or not isinstance(deletion_ms, int) or deletion_ms < 0:
            raise RetrievalProviderFailure("invalid_teardown_receipt")
        metrics = RetrievalLifecycleMetrics(deletion_ms=deletion_ms)
        return LifecyclePhaseEvidence(
            "completed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            metrics=metrics)
    except asyncio.TimeoutError:
        return LifecyclePhaseEvidence(
            "timed_out", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except QueryCancelled:
        return LifecyclePhaseEvidence(
            "cancelled", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    except RetrievalProviderUnavailable as exc:
        return LifecyclePhaseEvidence(
            "unavailable", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except RetrievalProviderFailure as exc:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code=exc.error_code)
    except Exception:
        return LifecyclePhaseEvidence(
            "failed", elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code="provider_error")


async def evaluate_retrieval_lifecycle(
    *,
    snapshot: DatasetSnapshot,
    adapters: Sequence[Any],
    timeout_seconds: float = 30.0,
    attempt_recovery: bool = False,
    perform_teardown: bool = False,
    cancellation: CancellationSignal | None = None,
) -> dict[str, Any]:
    """Observe lifecycle phases sequentially without selecting or activating."""
    if not isinstance(snapshot, DatasetSnapshot):
        raise TypeError("snapshot must be DatasetSnapshot")
    providers = tuple(adapters)
    if not 1 <= len(providers) <= MAX_LIFECYCLE_PROVIDERS:
        raise ValueError("adapters must contain 1..16 providers")
    profiles = tuple(getattr(item, "profile", None) for item in providers)
    if not all(isinstance(item, RetrievalProviderProfile) for item in profiles):
        raise TypeError("every adapter requires a RetrievalProviderProfile")
    if len({item.provider_id for item in profiles}) != len(profiles):
        raise ValueError("adapter provider IDs must be unique")
    if (isinstance(timeout_seconds, bool) or
            not MIN_TIMEOUT_SECONDS <= float(timeout_seconds) <= MAX_TIMEOUT_SECONDS):
        raise ValueError("timeout_seconds is outside the bounded range")
    signal = cancellation or CancellationSignal()
    evidence = []
    for adapter in providers:
        signal.checkpoint()
        baseline = await _lifecycle_phase(adapter, snapshot, signal, float(timeout_seconds))
        recovery = (await _recovery_phase(
            adapter, snapshot, signal, float(timeout_seconds))
            if attempt_recovery and baseline.status != "completed"
            else LifecyclePhaseEvidence("not_requested"))
        teardown = (await _teardown_phase(
            adapter, snapshot, signal, float(timeout_seconds))
            if perform_teardown else LifecyclePhaseEvidence("not_requested"))
        evidence.append(ProviderLifecycleEvidence(
            snapshot.snapshot_id, adapter.profile, baseline, recovery, teardown))
    return {
        "schema": "vera.retrieval-lifecycle-evidence/v1",
        "snapshot_id": snapshot.snapshot_id,
        "providers": [item.to_dict() for item in evidence],
        "attempt_recovery": bool(attempt_recovery),
        "perform_teardown": bool(perform_teardown),
        "winner": None,
        "fallback_selected": False,
        "activation_authority": False,
    }
