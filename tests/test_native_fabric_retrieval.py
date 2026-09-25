import asyncio
import hashlib

import pytest

from Vera.vera.fabric.dataset_provider import (
    CancellationSignal,
    DatasetSnapshot,
    QueryCancelled,
)
from Vera.vera.fabric.native_retrieval import (
    NativeFabricSnapshotProjection,
    NativeFabricVectorRetrievalAdapter,
)
from Vera.vera.fabric.projection_provider import EmbeddingSpace
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


def _space(dimension=3):
    return EmbeddingSpace(
        model_package_id="modelpkg_fixture_embed_v1",
        dimension=dimension,
        preprocessing="fixture-v1",
        metric="cosine",
    )


def _fixture():
    records = [
        {"record_id": "r1", "revision_id": "rev-a", "text": "alpha"},
        {"record_id": "r2", "revision_id": "rev-b", "text": "beta"},
        {"record_id": "r3", "revision_id": "rev-c", "text": "gamma"},
    ]
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="retrieval-corpus",
        created_at="2026-09-25T12:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string", "text": "string"},
        provenance={"source": "comparison-fixture"},
    )
    projection = NativeFabricSnapshotProjection(
        snapshot=snapshot,
        records=frozen,
        vectors_by_record_id={
            "r1": [1.0, 0.0, 0.0],
            "r2": [0.0, 1.0, 0.0],
            "r3": [0.0, 0.0, 1.0],
        },
        embedding=_space(),
    )
    return snapshot, frozen, projection


def test_projection_binds_complete_snapshot_and_redacts_content():
    snapshot, _, projection = _fixture()
    receipt = projection.receipt()
    assert projection.verify(snapshot)
    assert receipt["snapshot_id"] == snapshot.snapshot_id
    assert receipt["record_count"] == receipt["vector_count"] == 3
    assert receipt["dimension"] == 3
    assert receipt["projection_digest"].startswith("sha256:")
    assert receipt["activation_authority"] is False
    assert "alpha" not in repr(receipt)
    assert "alpha" not in repr(projection)


def test_adapter_returns_exact_revision_citations_and_comparison_evidence():
    snapshot, _, projection = _fixture()
    adapter = NativeFabricVectorRetrievalAdapter(
        projection=projection, embedding=_space(),
        embed_query=lambda _: [0.0, 0.95, 0.05])
    case = RetrievalCase(
        case_key="beta",
        query_digest=_digest("private beta query"),
        relevant_citations=(RetrievalCitation("r2", "rev-b"),),
        k=2,
    )
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "private beta query"),),
        adapters=(
            adapter,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("qdrant", "qdrant", "not-configured")),
        ),
    ))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    native = evidence["fabric_vector_snapshot"]
    assert native.observations[0].citations[0] == RetrievalCitation("r2", "rev-b")
    assert native.snapshot_id == snapshot.snapshot_id
    assert native.lifecycle.storage_bytes > 0
    assert result.report["winner"] is None
    assert adapter.receipt()["activation_authority"] is False
    assert "private beta query" not in repr(result.to_dict())


def test_async_embedder_is_supported_without_exposing_query():
    snapshot, _, projection = _fixture()
    seen = []

    async def embed(text):
        seen.append(text)
        return [1.0, 0.0, 0.0]

    adapter = NativeFabricVectorRetrievalAdapter(
        projection=projection, embedding=_space(), embed_query=embed)
    case = RetrievalCase(
        case_key="alpha", query_digest=_digest("private alpha"),
        relevant_citations=(RetrievalCitation("r1", "rev-a"),), k=1)
    citations = asyncio.run(adapter.retrieve(
        snapshot, RetrievalQueryBinding(case, "private alpha"), CancellationSignal()))
    assert citations == (RetrievalCitation("r1", "rev-a"),)
    assert seen == ["private alpha"]
    assert "private alpha" not in repr(adapter.receipt())


def test_adapter_rejects_a_different_query_embedding_space():
    _, _, projection = _fixture()
    other = EmbeddingSpace(
        model_package_id="modelpkg_other",
        dimension=3,
        preprocessing="fixture-v1",
        metric="cosine",
    )
    with pytest.raises(ValueError, match="embedding space"):
        NativeFabricVectorRetrievalAdapter(
            projection=projection, embedding=other,
            embed_query=lambda _: [1, 0, 0])


def test_records_must_recreate_the_exact_snapshot():
    snapshot, records, _ = _fixture()
    changed = [dict(item) for item in records]
    changed[0]["text"] = "changed"
    with pytest.raises(ValueError, match="recreate"):
        NativeFabricSnapshotProjection(
            snapshot=snapshot, records=changed,
            vectors_by_record_id={"r1": [1, 0], "r2": [0, 1], "r3": [1, 1]},
            embedding=_space(2))


@pytest.mark.parametrize("vectors, message", [
    ({"r1": [1, 0], "r2": [0, 1]}, "exactly"),
    ({"r1": [1, 0], "r2": [0, 1], "r3": [1, 0, 0]}, "dimensions"),
    ({"r1": [1, 0], "r2": [0, 1], "r3": [0, 0]}, "non-zero"),
    ({"r1": [1, 0], "r2": [0, 1], "r3": [float("inf"), 0]}, "finite"),
])
def test_projection_rejects_incomplete_or_invalid_vectors(vectors, message):
    snapshot, records, _ = _fixture()
    with pytest.raises(ValueError, match=message):
        NativeFabricSnapshotProjection(
            snapshot=snapshot, records=records, vectors_by_record_id=vectors,
            embedding=_space(2))


def test_duplicate_record_ids_are_rejected_even_with_distinct_revisions():
    records = [
        {"record_id": "same", "revision_id": "rev-a", "text": "a"},
        {"record_id": "same", "revision_id": "rev-b", "text": "b"},
    ]
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="duplicates", created_at="2026-09-25T12:00:00Z",
        records=records, schema={}, provenance={})
    with pytest.raises(ValueError, match="unique"):
        NativeFabricSnapshotProjection(
            snapshot=snapshot, records=frozen,
            vectors_by_record_id={"same": [1, 0]},
            embedding=_space(2))


def test_integrity_is_checked_again_after_embedding():
    snapshot, _, projection = _fixture()

    def tampering_embed(_):
        projection._vectors = projection._vectors[:-1] + ((0.5, 0.5, 0.5),)
        return [1.0, 0.0, 0.0]

    adapter = NativeFabricVectorRetrievalAdapter(
        projection=projection, embedding=_space(), embed_query=tampering_embed)
    case = RetrievalCase(
        case_key="tamper", query_digest=_digest("query"),
        relevant_citations=(RetrievalCitation("r1", "rev-a"),), k=1)
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "query"),),
        adapters=(
            adapter,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "analytical", "none")),
        ),
    ))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    assert evidence["fabric_vector_snapshot"].observations[0].error_code == \
        "projection_integrity_failed"


def test_teardown_is_idempotent_and_makes_projection_ineligible():
    snapshot, _, projection = _fixture()
    adapter = NativeFabricVectorRetrievalAdapter(
        projection=projection, embedding=_space(), embed_query=lambda _: [1, 0, 0])
    first = asyncio.run(adapter.teardown())
    second = asyncio.run(adapter.teardown())
    assert first == second
    assert first["active"] is False
    assert first["deletion_ms"] is not None
    assert projection._record_json == projection._citations == projection._vectors == ()
    assert not projection.verify(snapshot)

    case = RetrievalCase(
        case_key="deleted", query_digest=_digest("query"),
        relevant_citations=(RetrievalCitation("r1", "rev-a"),), k=1)
    result = asyncio.run(execute_retrieval_comparison(
        snapshot=snapshot,
        bindings=(RetrievalQueryBinding(case, "query"),),
        adapters=(
            adapter,
            UnavailableRetrievalAdapter(
                RetrievalProviderProfile("other", "analytical", "none")),
        ),
    ))
    evidence = {item.profile.provider_id: item for item in result.fixture.evidence}
    assert evidence["fabric_vector_snapshot"].observations[0].error_code == \
        "snapshot_unavailable"


def test_pre_cancelled_query_never_invokes_embedder():
    snapshot, _, projection = _fixture()
    called = []
    adapter = NativeFabricVectorRetrievalAdapter(
        projection=projection, embedding=_space(),
        embed_query=lambda text: called.append(text))
    case = RetrievalCase(
        case_key="cancelled", query_digest=_digest("query"),
        relevant_citations=(RetrievalCitation("r1", "rev-a"),), k=1)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled, match="cancelled"):
        asyncio.run(adapter.retrieve(
            snapshot, RetrievalQueryBinding(case, "query"), signal))
    assert called == []
