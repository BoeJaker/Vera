import asyncio

import pytest

from vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot
from vera.fabric.external_retrieval import (
    ExternalRetrievalRequest,
    ExternalSnapshotBinding,
    external_adapter,
)
from vera.fabric.qdrant_retrieval import QdrantSnapshotDriver, QdrantVectorMaterial
from vera.fabric.retrieval_lifecycle import evaluate_retrieval_lifecycle
from vera.fabric.retrieval_execution import RetrievalProviderFailure, RetrievalProviderUnavailable


pytestmark = pytest.mark.critical


RECORDS = (
    {"record_id": "rec-a", "revision_id": "rev-a", "text": "alpha"},
    {"record_id": "rec-b", "revision_id": "rev-b", "text": "beta"},
)


def snapshot():
    return DatasetSnapshot.create(
        dataset_id="qdrant", created_at="2026-09-26T00:00:00Z",
        records=RECORDS, schema={}, provenance={})[0]


def binding(mode="dense"):
    return ExternalSnapshotBinding.create(
        snapshot=snapshot(), records=RECORDS, kind="qdrant",
        provider_revision="qdrant-http-v1", mode=mode,
        projection_revision="vectors-v1")


def material(mode):
    dense = (1.0, 0.0)
    sparse = {"indices": [1, 4], "values": [0.5, 1.0]}
    multi = ((1.0, 0.0), (0.0, 1.0))
    return QdrantVectorMaterial(
        dense=dense if mode in {"dense", "hybrid"} else None,
        sparse=sparse if mode in {"sparse", "hybrid"} else None,
        multivector=multi if mode == "multivector" else None)


class Transport:
    def __init__(self, bound):
        self.bound = bound
        self.calls = []
        self.points = 0

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "PUT" and path.endswith("?wait=true"):
            self.points = len(body["points"])
        if method == "GET":
            return {"status": "ok", "result": {
                "points_count": self.points, "disk_data_size": 4096}}
        if method == "POST":
            return {"status": "ok", "result": {"points": [{"payload": {
                "snapshot_id": self.bound.snapshot.snapshot_id,
                "record_id": "rec-a", "revision_id": "rev-a"}}]}}
        return {"status": "ok", "result": True}


def driver(mode="dense"):
    bound = binding(mode)
    transport = Transport(bound)
    vectors = {item["record_id"]: material(mode) for item in RECORDS}
    subject = QdrantSnapshotDriver(
        binding=bound, transport=transport,
        vectors_by_record_id=vectors,
        encode_query=lambda text, supplied_mode: material(supplied_mode))
    return subject, bound, transport


def request_for(bound):
    return ExternalRetrievalRequest(
        snapshot_id=bound.snapshot.snapshot_id,
        projection_id=bound.projection_id,
        provider_revision=bound.provider_revision,
        mode=bound.mode,
        limit=2,
        _query_text="private query")


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid", "multivector"])
def test_provision_and_query_all_explicit_modes(mode):
    subject, bound, transport = driver(mode)
    receipt = subject.provision(cancellation=CancellationSignal())
    assert receipt["snapshot_id"] == bound.snapshot.snapshot_id
    create = transport.calls[0][2]
    if mode == "dense":
        assert create["vectors"]["dense"]["size"] == 2
    elif mode == "sparse":
        assert create["vectors"] == {}
        assert create["sparse_vectors"] == {"sparse": {}}
    elif mode == "hybrid":
        assert "dense" in create["vectors"] and "sparse" in create["sparse_vectors"]
    else:
        assert create["vectors"]["multivector"]["multivector_config"] == {
            "comparator": "max_sim"}
    result = subject.query(request_for(bound), cancellation=CancellationSignal())
    assert result["matches"] == [{"record_id": "rec-a", "revision_id": "rev-a"}]
    query = next(body for method, path, body in transport.calls if method == "POST")
    assert query["filter"]["must"][0]["match"]["value"] == bound.snapshot.snapshot_id
    if mode == "hybrid":
        assert query["query"] == {"fusion": "rrf"}
        assert [item["using"] for item in query["prefetch"]] == ["dense", "sparse"]


def test_points_use_deterministic_ids_and_revision_payloads():
    subject, bound, transport = driver()
    subject.provision(cancellation=CancellationSignal())
    upload = next(body for method, path, body in transport.calls
                  if method == "PUT" and path.endswith("?wait=true"))
    assert len({point["id"] for point in upload["points"]}) == 2
    assert upload["points"][0]["payload"] == {
        "snapshot_id": bound.snapshot.snapshot_id,
        "record_id": "rec-a", "revision_id": "rev-a"}


def test_lifecycle_recovery_and_teardown_are_exact():
    subject, bound, _ = driver()
    with pytest.raises(RetrievalProviderUnavailable, match="qdrant_unavailable"):
        subject.lifecycle(bound, cancellation=CancellationSignal())
    subject.provision(cancellation=CancellationSignal())
    lifecycle = subject.lifecycle(bound, cancellation=CancellationSignal())
    assert lifecycle["metrics"]["storage_bytes"] == 4096
    subject.recover(bound, CancellationSignal())
    receipt = subject.teardown(bound, cancellation=CancellationSignal())
    assert receipt["snapshot_id"] == bound.snapshot.snapshot_id
    assert receipt["active"] is False
    assert receipt["deletion_ms"] >= 0
    assert receipt["activation_authority"] is False


def test_external_adapter_and_lifecycle_coordinator_use_same_collection():
    subject, bound, _ = driver()
    subject.provision(cancellation=CancellationSignal())
    adapter = external_adapter(binding=bound, driver=subject)
    report = asyncio.run(evaluate_retrieval_lifecycle(
        snapshot=bound.snapshot, adapters=(adapter,), perform_teardown=True))
    row = report["providers"][0]
    assert row["baseline"]["status"] == "completed"
    assert row["teardown"]["status"] == "completed"
    assert row["teardown"]["metrics"]["deletion_ms"] >= 0
    assert report["activation_authority"] is False


def test_query_before_provision_and_identity_drift_fail_closed():
    subject, bound, _ = driver()
    with pytest.raises(RetrievalProviderUnavailable, match="qdrant_unavailable"):
        subject.query(request_for(bound), cancellation=CancellationSignal())
    subject.provision(cancellation=CancellationSignal())
    changed = ExternalRetrievalRequest(
        snapshot_id=bound.snapshot.snapshot_id,
        projection_id="xproj_" + "0" * 64,
        provider_revision=bound.provider_revision, mode=bound.mode,
        limit=1, _query_text="query")
    with pytest.raises(RetrievalProviderFailure, match="receipt_identity_mismatch"):
        subject.query(changed, cancellation=CancellationSignal())


def test_vector_coverage_dimensions_sparse_shape_and_encoding_are_bounded():
    bound = binding()
    with pytest.raises(ValueError, match="cover exactly"):
        QdrantSnapshotDriver(
            binding=bound, transport=Transport(bound),
            vectors_by_record_id={"rec-a": material("dense")},
            encode_query=lambda text, mode: material(mode))
    with pytest.raises(ValueError, match="dimensions"):
        QdrantSnapshotDriver(
            binding=bound, transport=Transport(bound),
            vectors_by_record_id={
                "rec-a": QdrantVectorMaterial(dense=(1.0, 0.0)),
                "rec-b": QdrantVectorMaterial(dense=(1.0, 0.0, 0.0))},
            encode_query=lambda text, mode: material(mode))
    sparse_bound = binding("sparse")
    with pytest.raises(ValueError, match="increasing"):
        QdrantSnapshotDriver(
            binding=sparse_bound, transport=Transport(sparse_bound),
            vectors_by_record_id={item["record_id"]: QdrantVectorMaterial(
                sparse={"indices": [2, 1], "values": [1.0, 1.0]}) for item in RECORDS},
            encode_query=lambda text, mode: material(mode))


def test_point_count_and_snapshot_payload_mismatch_fail_closed():
    subject, bound, transport = driver()
    transport.points = 99
    # Provision overwrites the count, so corrupt the GET response instead.
    original = transport.request
    def wrong_count(method, path, body=None):
        value = original(method, path, body)
        if method == "GET":
            value["result"]["points_count"] = 1
        return value
    transport.request = wrong_count
    with pytest.raises(RetrievalProviderFailure, match="point_count_mismatch"):
        subject.provision(cancellation=CancellationSignal())

    subject, bound, transport = driver()
    subject.provision(cancellation=CancellationSignal())
    original = transport.request
    def wrong_snapshot(method, path, body=None):
        value = original(method, path, body)
        if method == "POST":
            value["result"]["points"][0]["payload"]["snapshot_id"] = "wrong"
        return value
    transport.request = wrong_snapshot
    with pytest.raises(RetrievalProviderFailure, match="snapshot_mismatch"):
        subject.query(request_for(bound), cancellation=CancellationSignal())


def test_collection_status_failure_is_not_treated_as_valid_info():
    subject, bound, transport = driver()
    original = transport.request
    def failed_info(method, path, body=None):
        value = original(method, path, body)
        if method == "GET":
            value["status"] = "error"
        return value
    transport.request = failed_info
    with pytest.raises(RetrievalProviderFailure, match="operation_failed"):
        subject.provision(cancellation=CancellationSignal())
