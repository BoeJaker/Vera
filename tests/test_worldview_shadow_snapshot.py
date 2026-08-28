import pytest

from vera.fabric.projection_provider import EmbeddingSpace, ProjectionSpec
from vera.worldview.worldview_projection_adapter import WorldviewProjectionManifest
from vera.worldview.worldview_shadow_snapshot import (
    assemble_legacy_worldview_shadow_snapshot,
)


pytestmark = pytest.mark.critical


def _manifest():
    graph = ProjectionSpec("graph", "neo4j", "schema-v1")
    vector = ProjectionSpec(
        "vector", "chroma", "vector-v1",
        EmbeddingSpace("model", 2, "unit", "cosine"))
    return WorldviewProjectionManifest(
        manifest_id="manifest-1",
        graph_spec_id=graph.spec_id, graph_generation=1,
        vector_spec_id=vector.spec_id, vector_generation=1,
        embedding_model_package_id="model", embedding_dimension=2,
        preprocessing="unit", metric="cosine", record_count=1,
        record_revisions=(("r1", "rev_1"),),
        record_content_hashes=(("r1", "a" * 64),), tombstone_count=0,
        graph_snapshot_hash="b" * 64, vector_snapshot_hash="c" * 64)


def test_snapshot_copies_only_parity_inputs_and_compares():
    ids = ["r1"]
    vectors = [[1.0, 0.0]]
    meta = {"r1": {"revision_id": "rev_1", "content_hash": "a" * 64,
                    "text": "must not enter snapshot"}}
    edges = []
    snapshot = assemble_legacy_worldview_shadow_snapshot(
        record_ids=ids, embeddings=vectors, metadata=meta, edges=edges)
    vectors[0][0] = 9.0
    meta["r1"]["revision_id"] = "changed"
    assert snapshot.records[0].embedding == (1.0, 0.0)
    assert not hasattr(snapshot.records[0], "text")
    assert snapshot.compare(_manifest()).ready_for_shadow_comparison


def test_shape_duplicates_bounds_and_malformed_edges_fail_closed():
    with pytest.raises(ValueError, match="aligned"):
        assemble_legacy_worldview_shadow_snapshot(
            record_ids=["r1"], embeddings=[], metadata={}, edges=[])
    with pytest.raises(ValueError, match="duplicate"):
        assemble_legacy_worldview_shadow_snapshot(
            record_ids=["r1", "r1"], embeddings=[[], []], metadata={}, edges=[])
    with pytest.raises(ValueError, match="record limit"):
        assemble_legacy_worldview_shadow_snapshot(
            record_ids=["r1", "r2"], embeddings=[[], []], metadata={}, edges=[],
            max_records=1)
    with pytest.raises(ValueError, match="edges"):
        assemble_legacy_worldview_shadow_snapshot(
            record_ids=[], embeddings=[], metadata={}, edges=[("r1", "r2")])


def test_invalid_vectors_and_missing_provenance_are_preserved_as_failed_evidence():
    snapshot = assemble_legacy_worldview_shadow_snapshot(
        record_ids=["r1"], embeddings=[None], metadata={"r1": {"text": "private"}},
        edges=[])
    report = snapshot.compare(_manifest())
    assert report.invalid_embedding_record_ids == ("r1",)
    assert report.missing_revision_evidence_record_ids == ("r1",)
    assert not report.ready_for_shadow_comparison


def test_cancellation_is_checked_during_copy():
    calls = iter((False, False, True))
    with pytest.raises(RuntimeError, match="cancelled"):
        assemble_legacy_worldview_shadow_snapshot(
            record_ids=["r1", "r2"], embeddings=[[1.0], [2.0]], metadata={}, edges=[],
            cancelled=lambda: next(calls))
