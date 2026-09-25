import asyncio
import hashlib

import pytest

from Vera.vera.fabric.dataset_provider import CancellationSignal, DatasetSnapshot, QueryCancelled
from Vera.vera.fabric.native_retrieval import (
    NativeFabricGraphRetrievalAdapter,
    NativeFabricSnapshotGraphProjection,
    SnapshotGraphEdge,
)
from Vera.vera.fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalCitation,
    RetrievalProviderProfile,
)
from Vera.vera.fabric.retrieval_execution import (
    RetrievalQueryBinding,
    UnavailableRetrievalAdapter,
    execute_retrieval_comparison,
)


pytestmark = pytest.mark.critical


def _digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _fixture(*, directed=False, max_hops=1):
    records = [
        {"record_id": "r-alpha", "revision_id": "rev-a", "text": "alpha seed"},
        {"record_id": "r-beta", "revision_id": "rev-b", "text": "connected beta"},
        {"record_id": "r-gamma", "revision_id": "rev-c", "text": "remote gamma"},
        {"record_id": "r-other", "revision_id": "rev-d", "text": "unrelated"},
    ]
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="graph-corpus", created_at="2026-09-25T13:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string", "text": "string"},
        provenance={"source": "graph-fixture"})
    projection = NativeFabricSnapshotGraphProjection(
        snapshot=snapshot, records=frozen, max_hops=max_hops,
        edges=(
            SnapshotGraphEdge("r-alpha", "r-beta", "supports", directed),
            SnapshotGraphEdge("r-beta", "r-gamma", "extends", directed),
        ))
    return snapshot, frozen, projection


def _case(text="alpha", k=4):
    return RetrievalCase(
        case_key="graph-case", query_digest=_digest(text),
        relevant_citations=(RetrievalCitation("r-alpha", "rev-a"),), k=k)


def test_graph_projection_binds_snapshot_edges_and_redacts_records():
    snapshot, _, projection = _fixture()
    receipt = projection.receipt()
    assert projection.verify(snapshot)
    assert receipt["projection_spec"]["kind"] == "graph"
    assert receipt["record_count"] == 4
    assert receipt["edge_count"] == 2
    assert receipt["activation_authority"] is False
    assert "alpha seed" not in repr(receipt)
    assert "alpha seed" not in repr(projection)


def test_graph_retrieval_returns_seed_then_one_hop_revision_citations():
    snapshot, _, projection = _fixture(max_hops=1)
    adapter = NativeFabricGraphRetrievalAdapter(projection=projection)
    citations = asyncio.run(adapter.retrieve(
        snapshot, RetrievalQueryBinding(_case(), "alpha"), CancellationSignal()))
    assert citations == (
        RetrievalCitation("r-alpha", "rev-a"),
        RetrievalCitation("r-beta", "rev-b"),
    )


def test_graph_retrieval_obeys_hop_bound_and_stable_order():
    snapshot, _, projection = _fixture(max_hops=2)
    citations = projection.search("alpha", 4)
    assert citations == (
        RetrievalCitation("r-alpha", "rev-a"),
        RetrievalCitation("r-beta", "rev-b"),
        RetrievalCitation("r-gamma", "rev-c"),
    )


def test_directed_edges_do_not_traverse_backwards():
    _, _, projection = _fixture(directed=True, max_hops=2)
    assert projection.search("gamma", 4) == (RetrievalCitation("r-gamma", "rev-c"),)


@pytest.mark.parametrize("text, limit, message", [
    ("alpha", 0, "limit"),
    ("alpha", True, "limit"),
    ("x" * (64 * 1024 + 1), 1, "size"),
])
def test_graph_query_is_bounded(text, limit, message):
    _, _, projection = _fixture()
    with pytest.raises(ValueError, match=message):
        projection.search(text, limit)


def test_executor_records_graph_evidence_without_winner_or_query_text():
    snapshot, _, projection = _fixture()
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(_case("private alpha"), "private alpha"),),
        adapters=(
            NativeFabricGraphRetrievalAdapter(projection=projection),
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("graphrag", "graphrag", "not-configured")),
        )))
    graph = {item.profile.provider_id: item for item in result.fixture.evidence}[
        "fabric_graph_snapshot"]
    assert graph.observations[0].citations[0] == RetrievalCitation("r-alpha", "rev-a")
    assert graph.lifecycle.storage_bytes > 0
    assert result.report["winner"] is None
    assert "private alpha" not in repr(result.to_dict())


@pytest.mark.parametrize("edges, message", [
    ((SnapshotGraphEdge("r-alpha", "missing"),), "endpoint"),
    ((SnapshotGraphEdge("r-alpha", "r-beta"),
      SnapshotGraphEdge("r-alpha", "r-beta")), "unique"),
])
def test_graph_rejects_unknown_endpoints_and_duplicate_edges(edges, message):
    snapshot, records, _ = _fixture()
    with pytest.raises(ValueError, match=message):
        NativeFabricSnapshotGraphProjection(
            snapshot=snapshot, records=records, edges=edges)


def test_edge_identity_rejects_self_edges_and_invalid_direction_flag():
    with pytest.raises(ValueError, match="self edges"):
        SnapshotGraphEdge("same", "same")
    with pytest.raises(TypeError, match="directed"):
        SnapshotGraphEdge("a", "b", directed=1)


def test_teardown_clears_graph_material_and_is_idempotent():
    snapshot, _, projection = _fixture()
    adapter = NativeFabricGraphRetrievalAdapter(projection=projection)
    first = asyncio.run(adapter.teardown())
    second = asyncio.run(adapter.teardown())
    assert first == second
    assert first["active"] is False and first["deletion_ms"] is not None
    assert projection._record_json == projection._citations == projection._edges == ()
    assert not projection.verify(snapshot)

    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(_case(), "alpha"),),
        adapters=(
            adapter,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "analytical", "none")),
        )))
    graph = {item.profile.provider_id: item for item in result.fixture.evidence}[
        "fabric_graph_snapshot"]
    assert graph.observations[0].error_code == "snapshot_unavailable"


def test_pre_cancelled_graph_query_does_not_search():
    snapshot, _, projection = _fixture()
    adapter = NativeFabricGraphRetrievalAdapter(projection=projection)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled, match="cancelled"):
        asyncio.run(adapter.retrieve(
            snapshot, RetrievalQueryBinding(_case(), "alpha"), signal))


def test_graph_integrity_is_checked_after_search():
    snapshot, _, projection = _fixture()
    original = projection.search

    def tampering_search(text, limit):
        result = original(text, limit)
        projection._edges = ()
        return result

    projection.search = tampering_search
    adapter = NativeFabricGraphRetrievalAdapter(projection=projection)
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(_case(), "alpha"),),
        adapters=(
            adapter,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "analytical", "none")),
        )))
    graph = {item.profile.provider_id: item for item in result.fixture.evidence}[
        "fabric_graph_snapshot"]
    assert graph.observations[0].error_code == "projection_integrity_failed"
