import asyncio
import hashlib

import pytest

from vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot
from vera.fabric.external_retrieval import (
    ExternalSnapshotBinding,
    external_adapter,
)
from vera.fabric.retrieval_comparison import RetrievalCase, RetrievalCitation
from vera.fabric.retrieval_execution import (
    RetrievalProviderFailure,
    RetrievalProviderUnavailable,
    RetrievalQueryBinding,
    execute_retrieval_comparison,
)


RECORDS = (
    {"record_id": "rec-a", "revision_id": "rev-a", "text": "alpha"},
    {"record_id": "rec-b", "revision_id": "rev-b", "text": "beta"},
)


def fixture_snapshot():
    return DatasetSnapshot.create(
        dataset_id="external-retrieval", created_at="2026-09-25T00:00:00Z",
        records=RECORDS, schema={"type": "object"}, provenance={"source": "test"},
    )[0]


def make_binding(kind="qdrant", mode="hybrid"):
    return ExternalSnapshotBinding.create(
        snapshot=fixture_snapshot(), records=RECORDS, kind=kind,
        provider_revision=f"{kind}-1.0", mode=mode,
        projection_revision="projection-abc123",
    )


def identity(binding):
    return {
        "schema": "vera.external-snapshot-retrieval/v1",
        "snapshot_id": binding.snapshot.snapshot_id,
        "projection_id": binding.projection_id,
        "provider_revision": binding.provider_revision,
        "mode": binding.mode,
    }


class Driver:
    def __init__(self, binding, *, mutate=None):
        self.binding = binding
        self.mutate = mutate or (lambda value: value)
        self.query_text = None

    def query(self, request, *, cancellation):
        cancellation.checkpoint()
        self.query_text = request.query_text
        return self.mutate({
            **identity(self.binding),
            "matches": [{"record_id": "rec-a", "revision_id": "rev-a"}],
        })

    def lifecycle(self, binding, *, cancellation):
        cancellation.checkpoint()
        return self.mutate({
            **identity(self.binding),
            "metrics": {"index_ms": 3, "update_ms": None,
                        "storage_bytes": 512, "rebuild_ms": 4,
                        "deletion_ms": 2},
        })


def query_binding(text="alpha"):
    case = RetrievalCase(
        case_key="external-case",
        query_digest="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        relevant_citations=(RetrievalCitation("rec-a", "rev-a"),), k=2,
    )
    return RetrievalQueryBinding(case, text)


@pytest.mark.parametrize("kind,mode", [
    ("qdrant", "dense"), ("qdrant", "sparse"),
    ("qdrant", "hybrid"), ("qdrant", "multivector"),
    ("graphrag", "local"), ("graphrag", "global"),
    ("graphrag", "drift"),
])
def test_supported_modes_are_explicit(kind, mode):
    binding = make_binding(kind, mode)
    assert binding.profile.kind == kind
    assert binding.mode == mode


def test_exact_query_and_lifecycle_receipts_feed_executor():
    binding = make_binding()
    graph_binding = make_binding("graphrag", "local")
    driver = Driver(binding)
    graph_driver = Driver(graph_binding)
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=binding.snapshot, bindings=(query_binding(),),
        adapters=(
            external_adapter(binding=binding, driver=driver),
            external_adapter(binding=graph_binding, driver=graph_driver),
        ),
    ))
    evidence = next(item for item in result.fixture.evidence
                    if item.profile.kind == "qdrant")
    assert evidence.observations[0].citations == (
        RetrievalCitation("rec-a", "rev-a"),)
    assert evidence.lifecycle.storage_bytes == 512
    assert driver.query_text == "alpha"
    assert graph_driver.query_text == "alpha"
    assert "alpha" not in repr(query_binding())


def test_absent_runtime_is_unavailable_without_fallback():
    binding = make_binding("graphrag", "local")
    adapter = external_adapter(binding=binding, driver=None)
    with pytest.raises(RetrievalProviderUnavailable, match="graphrag_unavailable"):
        asyncio.run(adapter.retrieve(
            binding.snapshot, query_binding(), CancellationSignal()))


@pytest.mark.parametrize("field,value", [
    ("snapshot_id", "snap_" + "0" * 64),
    ("projection_id", "xproj_" + "0" * 64),
    ("provider_revision", "other-1.0"),
    ("mode", "dense"),
])
def test_query_receipt_identity_drift_fails_closed(field, value):
    binding = make_binding()
    driver = Driver(binding, mutate=lambda row: {**row, field: value})
    adapter = external_adapter(binding=binding, driver=driver)
    with pytest.raises(RetrievalProviderFailure, match="receipt_identity_mismatch"):
        asyncio.run(adapter.retrieve(
            binding.snapshot, query_binding(), CancellationSignal()))


def test_citation_must_be_revision_bound_inside_snapshot():
    binding = make_binding()
    driver = Driver(binding, mutate=lambda row: {
        **row, "matches": [{"record_id": "rec-a", "revision_id": "rev-old"}]})
    with pytest.raises(RetrievalProviderFailure, match="citation_outside_snapshot"):
        asyncio.run(external_adapter(binding=binding, driver=driver).retrieve(
            binding.snapshot, query_binding(), CancellationSignal()))


def test_duplicate_and_oversized_results_fail_closed():
    binding = make_binding()
    duplicate = {"record_id": "rec-a", "revision_id": "rev-a"}
    driver = Driver(binding, mutate=lambda row: {**row, "matches": [duplicate, duplicate]})
    with pytest.raises(RetrievalProviderFailure, match="duplicate_citation"):
        asyncio.run(external_adapter(binding=binding, driver=driver).retrieve(
            binding.snapshot, query_binding(), CancellationSignal()))

    three = Driver(binding, mutate=lambda row: {**row, "matches": [
        duplicate, {"record_id": "rec-b", "revision_id": "rev-b"}, duplicate]})
    with pytest.raises(RetrievalProviderFailure, match="result_limit_exceeded"):
        asyncio.run(external_adapter(binding=binding, driver=three).retrieve(
            binding.snapshot, query_binding(), CancellationSignal()))


def test_other_snapshot_and_incomplete_manifest_are_rejected():
    binding = make_binding()
    other = DatasetSnapshot.create(
        dataset_id="other", created_at="2026-09-25T00:00:00Z",
        records=RECORDS, schema={}, provenance={},
    )[0]
    with pytest.raises(RetrievalProviderUnavailable, match="snapshot_unavailable"):
        asyncio.run(external_adapter(binding=binding, driver=Driver(binding)).retrieve(
            other, query_binding(), CancellationSignal()))
    with pytest.raises(ValueError, match="recreate"):
        ExternalSnapshotBinding(
            snapshot=binding.snapshot, records=RECORDS[:1],
            kind="qdrant", provider_revision="qdrant-1",
            mode="dense", projection_revision="projection-1",
        )


def test_bad_lifecycle_metrics_are_classified_without_detail_leakage():
    binding = make_binding()
    driver = Driver(binding, mutate=lambda row: {
        **row, "metrics": {"index_ms": -1}} if "metrics" in row else row)
    with pytest.raises(RetrievalProviderFailure, match="invalid_lifecycle_receipt"):
        asyncio.run(external_adapter(binding=binding, driver=driver).lifecycle(
            binding.snapshot, CancellationSignal()))


def test_binding_recreates_exact_snapshot_and_rejects_modes():
    with pytest.raises(ValueError, match="recreate"):
        ExternalSnapshotBinding.create(
            snapshot=fixture_snapshot(), records=RECORDS[:1], kind="qdrant",
            provider_revision="qdrant-1", mode="dense",
            projection_revision="projection-1")
    with pytest.raises(ValueError, match="unsupported qdrant"):
        make_binding("qdrant", "local")
