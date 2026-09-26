import asyncio
import hashlib

import pytest

from vera.fabric.analytical_retrieval import (
    AnalyticalQueryPlan,
    AnalyticalSnapshotRetrievalAdapter,
)
from vera.fabric.dataset_provider import (
    CancellationSignal,
    DatasetSnapshot,
    QueryPage,
)
from vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalLifecycleMetrics,
)
from vera.fabric.retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
)


RECORDS = (
    {"record_id": "rec-a", "revision_id": "rev-a", "kind": "odd"},
    {"record_id": "rec-b", "revision_id": "rev-b", "kind": "even"},
)


def snapshot():
    return DatasetSnapshot.create(
        dataset_id="analytics", created_at="2026-09-26T00:00:00Z",
        records=RECORDS, schema={}, provenance={"artifact": "sha256:test"})[0]


def query_binding(text="odd rows"):
    case = RetrievalCase(
        case_key="odd", query_digest="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        relevant_citations=(RetrievalCitation("rec-a", "rev-a"),), k=2)
    return RetrievalQueryBinding(case, text)


class Provider:
    def __init__(self, snap, *, matches=({"record_index": 0},), page_snapshot=""):
        self.binding = type("Binding", (), {
            "snapshot_id": snap.snapshot_id, "dataset_id": snap.dataset_id})()
        self.matches = matches
        self.page_snapshot = page_snapshot
        self.request = None

    def query(self, request, *, cancellation):
        cancellation.checkpoint()
        self.request = request
        return QueryPage(
            query_id=request.query_id,
            snapshot_id=self.page_snapshot or request.snapshot_id,
            matches=tuple(self.matches), provider="analytical-test",
            next_cursor="",
            provenance={"mode": "structured-read-only"})


def adapter(provider=None):
    snap = snapshot()
    provider = provider or Provider(snap)
    binding = query_binding()
    return AnalyticalSnapshotRetrievalAdapter(
        snapshot=snap, records=RECORDS, provider=provider,
        provider_revision="duckdb-1.5.5",
        plans=(AnalyticalQueryPlan(binding.case.case_id, {"kind": "odd"}),),
        lifecycle=RetrievalLifecycleMetrics(storage_bytes=123)), provider, binding


def test_structured_plan_returns_revision_citation_without_text_or_data():
    subject, provider, binding = adapter()
    result = asyncio.run(subject.retrieve(
        subject.snapshot, binding, CancellationSignal()))
    assert result == (RetrievalCitation("rec-a", "rev-a"),)
    assert provider.request.text == ""
    assert provider.request.filters == {"kind": "odd"}
    assert provider.request.include_data is False
    lifecycle = asyncio.run(subject.lifecycle(subject.snapshot, CancellationSignal()))
    assert lifecycle.storage_bytes == 123


def test_provider_must_bind_exact_dataset_snapshot():
    snap = snapshot()
    provider = Provider(snap)
    provider.binding.snapshot_id = "snap_" + "0" * 64
    with pytest.raises(ValueError, match="exact snapshot"):
        AnalyticalSnapshotRetrievalAdapter(
            snapshot=snap, records=RECORDS, provider=provider,
            provider_revision="duckdb-1", plans=(
                AnalyticalQueryPlan(query_binding().case.case_id, {}),))


def test_missing_plan_and_other_snapshot_are_unavailable():
    subject, _, binding = adapter()
    other_case = RetrievalCase(
        case_key="other", query_digest="sha256:" + hashlib.sha256(b"x").hexdigest(),
        relevant_citations=(RetrievalCitation("rec-a", "rev-a"),), k=1)
    with pytest.raises(RetrievalProviderUnavailable, match="query_plan_unavailable"):
        asyncio.run(subject.retrieve(subject.snapshot,
                                     RetrievalQueryBinding(other_case, "x"),
                                     CancellationSignal()))
    other = DatasetSnapshot.create(
        dataset_id="other", created_at="2026-09-26T00:00:00Z",
        records=RECORDS, schema={}, provenance={})[0]
    with pytest.raises(RetrievalProviderUnavailable, match="snapshot_unavailable"):
        asyncio.run(subject.retrieve(other, binding, CancellationSignal()))


@pytest.mark.parametrize("matches,error", [
    (({"record_index": 3},), "invalid_citation"),
    (({"record_index": True},), "invalid_citation"),
    (({"record_index": 0}, {"record_index": 0}), "duplicate_citation"),
])
def test_invalid_and_duplicate_indexes_fail_closed(matches, error):
    snap = snapshot()
    subject, _, binding = adapter(Provider(snap, matches=matches))
    with pytest.raises(RetrievalProviderFailure, match=error):
        asyncio.run(subject.retrieve(snap, binding, CancellationSignal()))


def test_page_snapshot_drift_and_provider_failure_are_bounded():
    snap = snapshot()
    subject, _, binding = adapter(Provider(
        snap, page_snapshot="snap_" + "0" * 64))
    with pytest.raises(RetrievalProviderFailure, match="snapshot_mismatch"):
        asyncio.run(subject.retrieve(snap, binding, CancellationSignal()))

    class Broken(Provider):
        def query(self, request, *, cancellation):
            raise RuntimeError("secret database detail")

    subject, _, binding = adapter(Broken(snap))
    with pytest.raises(RetrievalProviderFailure, match="provider_error") as error:
        asyncio.run(subject.retrieve(snap, binding, CancellationSignal()))
    assert "secret" not in str(error.value)


def test_plans_are_bounded_unique_and_scalar():
    binding = query_binding()
    with pytest.raises(ValueError, match="unique"):
        AnalyticalSnapshotRetrievalAdapter(
            snapshot=snapshot(), records=RECORDS, provider=Provider(snapshot()),
            provider_revision="duckdb-1", plans=(
                AnalyticalQueryPlan(binding.case.case_id, {}),
                AnalyticalQueryPlan(binding.case.case_id, {})))
    with pytest.raises(ValueError, match="scalar"):
        AnalyticalQueryPlan(binding.case.case_id, {"kind": ["odd"]})
    with pytest.raises(ValueError, match="finite"):
        AnalyticalQueryPlan(binding.case.case_id, {"score": float("nan")})
    plan = AnalyticalQueryPlan(binding.case.case_id, {"kind": "odd"})
    with pytest.raises(TypeError):
        plan.filters["kind"] = "even"
