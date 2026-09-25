import asyncio
import hashlib

import pytest

from vera.fabric.dataset_provider import (
    CancellationSignal,
    DatasetSnapshot,
    FrozenDatasetProvider,
    QueryCancelled,
)
from vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalLifecycleMetrics,
    RetrievalProviderProfile,
)
from vera.fabric.retrieval_execution import (
    QueryProviderRetrievalAdapter,
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
    UnavailableRetrievalAdapter,
    execute_retrieval_comparison,
)


pytestmark = pytest.mark.critical


def _digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _fixture():
    records = (
        {"record_id": "r1", "revision_id": "v1", "text": "alpha needle"},
        {"record_id": "r2", "revision_id": "v2", "text": "beta"},
    )
    provider = FrozenDatasetProvider()
    snapshot = provider.register(
        dataset_id="retrieval-corpus",
        created_at="2026-09-21T12:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string", "text": "string"},
        provenance={"source": "test"},
    )
    case = RetrievalCase(
        case_key="case-a",
        query_digest=_digest("needle"),
        relevant_citations=(RetrievalCitation("r1", "v1"),),
        k=2,
    )
    return records, provider, snapshot, case


def _adapter(provider, records, provider_id="frozen"):
    return QueryProviderRetrievalAdapter(
        profile=RetrievalProviderProfile(provider_id, "analytical", "v1"),
        provider=provider,
        citations_by_record_index=tuple(
            RetrievalCitation(row["record_id"], row["revision_id"])
            for row in records
        ),
        lifecycle=RetrievalLifecycleMetrics(index_ms=7, storage_bytes=12),
    )


def test_executes_identical_snapshot_and_cases_without_serialising_query_text():
    records, provider, snapshot, case = _fixture()
    binding = RetrievalQueryBinding(case, "needle")
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(binding,),
        adapters=(
            _adapter(provider, records),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("qdrant", "qdrant", "not-configured")),
        ),
    ))

    public = result.to_dict()
    assert "needle" not in repr(binding)
    assert "needle" not in repr(public)
    assert public["report"]["providers_invoked"] is False
    assert public["report"]["winner"] is None
    assert public["report"]["providers"]["frozen"]["quality"]["recall_at_k"] == 1.0
    assert public["report"]["providers"]["qdrant"]["outcomes"]["failed"] == 1
    assert result.fixture.evidence[0].snapshot_id == snapshot.snapshot_id
    assert {row.observations[0].case_id for row in result.fixture.evidence} == {case.case_id}


def test_binding_rejects_query_material_that_does_not_match_digest():
    _, _, _, case = _fixture()
    with pytest.raises(ValueError, match="case digest"):
        RetrievalQueryBinding(case, "different")


@pytest.mark.parametrize("factory", [
    lambda: RetrievalProviderFailure("secret detail"),
    lambda: RetrievalProviderUnavailable("UPPER"),
    lambda: UnavailableRetrievalAdapter(
        RetrievalProviderProfile("provider", "qdrant", "v1"),
        error_code="x" * 65,
    ),
])
def test_adapter_error_codes_are_bounded_machine_identifiers(factory):
    with pytest.raises(ValueError, match="error code"):
        factory()


class _SlowAdapter:
    profile = RetrievalProviderProfile("slow", "graphrag", "v1")

    async def retrieve(self, snapshot, binding, cancellation):
        await asyncio.sleep(0.1)
        cancellation.checkpoint()
        return ()

    async def lifecycle(self, snapshot, cancellation):
        return RetrievalLifecycleMetrics()


class _LeakyFailureAdapter:
    profile = RetrievalProviderProfile("broken", "fabric_graph", "v1")

    async def retrieve(self, snapshot, binding, cancellation):
        raise RuntimeError("secret query and credential")

    async def lifecycle(self, snapshot, cancellation):
        raise RuntimeError("private backend detail")


def test_timeout_and_unexpected_error_are_bounded_and_redacted():
    _, _, snapshot, case = _fixture()
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "needle"),),
        adapters=(_SlowAdapter(), _LeakyFailureAdapter()),
        timeout_seconds=0.01,
    ))
    evidence = {row.profile.provider_id: row for row in result.fixture.evidence}
    assert evidence["slow"].observations[0].error_code == "timeout"
    assert evidence["broken"].observations[0].error_code == "provider_error"
    assert "secret" not in repr(result.to_dict())
    assert set(evidence["broken"].lifecycle.to_dict().values()) == {None}


class _WrongSnapshotProvider:
    def query(self, request, *, cancellation=None):
        class Page:
            snapshot_id = "snap_" + "0" * 64
            matches = ()
        return Page()


def test_query_provider_adapter_rejects_snapshot_mismatch():
    records, _, snapshot, case = _fixture()
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "needle"),),
        adapters=(
            _adapter(_WrongSnapshotProvider(), records, "wrong"),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "qdrant", "v1")),
        ),
    ))
    evidence = {row.profile.provider_id: row for row in result.fixture.evidence}
    assert evidence["wrong"].observations[0].error_code == "snapshot_mismatch"


def test_query_provider_adapter_requires_complete_snapshot_citation_map():
    records, provider, snapshot, case = _fixture()
    incomplete = QueryProviderRetrievalAdapter(
        profile=RetrievalProviderProfile("incomplete", "analytical", "v1"),
        provider=provider,
        citations_by_record_index=(RetrievalCitation("r1", "v1"),),
    )
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "needle"),),
        adapters=(
            incomplete,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "qdrant", "v1")),
        ),
    ))
    evidence = {row.profile.provider_id: row for row in result.fixture.evidence}
    assert evidence["incomplete"].observations[0].error_code == "citation_map_mismatch"


def test_pre_cancelled_execution_reports_cancellation_without_invocation():
    records, provider, snapshot, case = _fixture()
    signal = CancellationSignal()
    signal.cancel()
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "needle"),),
        adapters=(
            _adapter(provider, records),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "qdrant", "v1")),
        ),
        cancellation=signal,
    ))
    assert all(row.observations[0].status == "cancelled"
               for row in result.fixture.evidence)


class _CancellationAwareAdapter:
    profile = RetrievalProviderProfile("cancel-aware", "graphrag", "v1")

    async def retrieve(self, snapshot, binding, cancellation):
        while True:
            cancellation.checkpoint()
            await asyncio.sleep(0.005)

    async def lifecycle(self, snapshot, cancellation):
        return RetrievalLifecycleMetrics()


def test_cancellation_during_provider_call_is_observed_promptly():
    _, _, snapshot, case = _fixture()
    signal = CancellationSignal()

    async def run():
        task = asyncio.create_task(execute_retrieval_comparison(
            snapshot=snapshot,
            bindings=(RetrievalQueryBinding(case, "needle"),),
            adapters=(
                _CancellationAwareAdapter(),
                UnavailableRetrievalAdapter(
                    RetrievalProviderProfile("other", "qdrant", "v1")),
            ),
            timeout_seconds=2,
            cancellation=signal,
        ))
        await asyncio.sleep(0.02)
        signal.cancel()
        return await asyncio.wait_for(task, timeout=0.5)

    result = asyncio.run(run())
    assert all(row.observations[0].status == "cancelled"
               for row in result.fixture.evidence)


@pytest.mark.parametrize("timeout", [0, float("inf"), True, "1"])
def test_execution_rejects_unbounded_or_non_numeric_timeouts(timeout):
    records, provider, snapshot, case = _fixture()
    with pytest.raises((TypeError, ValueError), match="timeout_seconds"):
        asyncio.run(execute_retrieval_comparison(
            snapshot=snapshot,
            bindings=(RetrievalQueryBinding(case, "needle"),),
            adapters=(
                _adapter(provider, records),
                UnavailableRetrievalAdapter(
                    RetrievalProviderProfile("other", "qdrant", "v1")),
            ),
            timeout_seconds=timeout,
        ))
