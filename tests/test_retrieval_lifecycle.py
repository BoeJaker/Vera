import asyncio

import pytest

from vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot, QueryCancelled
from vera.fabric.retrieval_comparison import (
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from vera.fabric.retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
)
from vera.fabric.retrieval_lifecycle import evaluate_retrieval_lifecycle


pytestmark = pytest.mark.critical


def snapshot():
    return DatasetSnapshot.create(
        dataset_id="lifecycle", created_at="2026-09-26T00:00:00Z",
        records=({"record_id": "rec-a", "revision_id": "rev-a"},),
        schema={}, provenance={})[0]


class Adapter:
    def __init__(self, provider_id="provider", *, lifecycle=None,
                 recover=None, teardown=None):
        self.profile = RetrievalProviderProfile(provider_id, "analytical", "revision-1")
        self._lifecycle = lifecycle or (lambda: RetrievalLifecycleMetrics(index_ms=2))
        if recover is not None:
            self.recover = recover
        if teardown is not None:
            self.teardown = teardown

    async def lifecycle(self, snap, signal):
        signal.checkpoint()
        result = self._lifecycle()
        if isinstance(result, Exception):
            raise result
        if asyncio.iscoroutine(result):
            return await result
        return result


def provider(report, provider_id="provider"):
    return next(item for item in report["providers"]
                if item["profile"]["provider_id"] == provider_id)


def test_completed_lifecycle_is_distinct_from_unmeasured_values():
    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(Adapter(),)))
    row = provider(report)
    assert row["baseline"]["status"] == "completed"
    assert row["baseline"]["metrics"]["index_ms"] == 2
    assert row["baseline"]["metrics"]["deletion_ms"] is None
    assert row["recovery"]["status"] == "not_requested"
    assert row["teardown"]["status"] == "not_requested"
    assert report["winner"] is None
    assert report["fallback_selected"] is False
    assert report["activation_authority"] is False


@pytest.mark.parametrize("error,status,code", [
    (RetrievalProviderUnavailable("snapshot_unavailable"), "unavailable", "snapshot_unavailable"),
    (RetrievalProviderFailure("invalid_lifecycle_receipt"), "failed", "invalid_lifecycle_receipt"),
    (RuntimeError("secret backend detail"), "failed", "provider_error"),
    (QueryCancelled(), "cancelled", ""),
])
def test_lifecycle_failures_remain_distinguishable_and_redacted(error, status, code):
    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(Adapter(lifecycle=lambda: error),)))
    phase = provider(report)["baseline"]
    assert phase["status"] == status
    assert phase["error_code"] == code
    assert phase["metrics"] is None
    assert "secret" not in str(report)


def test_timeout_is_not_reported_as_null_success():
    async def slow():
        await asyncio.sleep(0.05)
        return RetrievalLifecycleMetrics()

    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(Adapter(lifecycle=slow),),
        timeout_seconds=0.01))
    assert provider(report)["baseline"]["status"] == "timed_out"


def test_recovery_requires_method_and_fresh_lifecycle_evidence():
    state = {"up": False}

    def lifecycle():
        return (RetrievalLifecycleMetrics(rebuild_ms=7) if state["up"]
                else RetrievalProviderUnavailable("provider_unavailable"))

    async def recover(snap, signal):
        signal.checkpoint()
        state["up"] = True

    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(Adapter(lifecycle=lifecycle, recover=recover),),
        attempt_recovery=True))
    row = provider(report)
    assert row["baseline"]["status"] == "unavailable"
    assert row["recovery"]["status"] == "completed"
    assert row["recovery"]["metrics"]["rebuild_ms"] == 7

    unsupported = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(
            Adapter(lifecycle=lambda: RetrievalProviderUnavailable("provider_unavailable")),),
        attempt_recovery=True))
    assert provider(unsupported)["recovery"]["status"] == "unsupported"


def test_synchronous_recovery_and_teardown_hooks_are_supported():
    snap = snapshot()
    state = {"up": False}

    def lifecycle():
        return (RetrievalLifecycleMetrics(rebuild_ms=2) if state["up"]
                else RetrievalProviderUnavailable("provider_unavailable"))

    def recover(supplied, signal):
        assert supplied == snap
        signal.checkpoint()
        state["up"] = True

    def teardown(*, cancellation):
        cancellation.checkpoint()
        return {"snapshot_id": snap.snapshot_id, "active": False, "deletion_ms": 1}

    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snap,
        adapters=(Adapter(lifecycle=lifecycle, recover=recover, teardown=teardown),),
        attempt_recovery=True, perform_teardown=True))
    row = provider(report)
    assert row["recovery"]["status"] == "completed"
    assert row["teardown"]["status"] == "completed"


def test_teardown_requires_exact_snapshot_inactive_and_deletion_measurement():
    snap = snapshot()

    async def teardown(*, cancellation):
        cancellation.checkpoint()
        return {"snapshot_id": snap.snapshot_id, "active": False, "deletion_ms": 3}

    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snap, adapters=(Adapter(teardown=teardown),),
        perform_teardown=True))
    phase = provider(report)["teardown"]
    assert phase["status"] == "completed"
    assert phase["metrics"]["deletion_ms"] == 3

    for receipt, code in [
        ({"snapshot_id": "snap_" + "0" * 64, "active": False, "deletion_ms": 1},
         "teardown_snapshot_mismatch"),
        ({"snapshot_id": snap.snapshot_id, "active": True, "deletion_ms": 1},
         "teardown_not_confirmed"),
        ({"snapshot_id": snap.snapshot_id, "active": False, "deletion_ms": None},
         "invalid_teardown_receipt"),
    ]:
        async def invalid(*, cancellation, value=receipt):
            return value
        bad = asyncio.run(evaluate_retrieval_lifecycle(
            snapshot=snap, adapters=(Adapter(teardown=invalid),),
            perform_teardown=True))
        assert provider(bad)["teardown"]["error_code"] == code


def test_provider_order_is_sequential_and_bounded():
    order = []

    def lifecycle(name):
        order.append(name)
        return RetrievalLifecycleMetrics()

    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=snapshot(), adapters=(
            Adapter("a", lifecycle=lambda: lifecycle("a")),
            Adapter("b", lifecycle=lambda: lifecycle("b")),
        )))
    assert order == ["a", "b"]
    assert [item["profile"]["provider_id"] for item in report["providers"]] == ["a", "b"]
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(evaluate_retrieval_lifecycle(
            snapshot=snapshot(), adapters=(Adapter("a"), Adapter("a"))))


def test_bounds_and_caller_cancellation():
    with pytest.raises(ValueError, match="1..16"):
        asyncio.run(evaluate_retrieval_lifecycle(snapshot=snapshot(), adapters=()))
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(evaluate_retrieval_lifecycle(
            snapshot=snapshot(), adapters=(Adapter(),), timeout_seconds=0))
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        asyncio.run(evaluate_retrieval_lifecycle(
            snapshot=snapshot(), adapters=(Adapter(),), cancellation=signal))
