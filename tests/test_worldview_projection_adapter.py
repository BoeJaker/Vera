import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.projection_provider import (
    EmbeddingSpace, ProjectionEntry, ProjectionSpec, build_projection_entry,
)
from vera.fabric.record_revision import create_record_revision
from vera.worldview.worldview_projection_adapter import WorldviewProjectionAdapter


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def specs(dimension=3):
    vector = ProjectionSpec(
        "vector", "chroma", "vector-v1",
        EmbeddingSpace("model.embed.v1", dimension, "utf8-nfc-v1", "cosine"))
    graph = ProjectionSpec("graph", "neo4j", "property-graph-v1")
    return vector, graph


def revision(seed="a", *, content="record", parents=(), tombstone=False):
    return create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id="rec_" + seed * 64, content=None if tombstone else content,
        parents=parents, tombstone=tombstone)


def entries(item, vector_spec, graph_spec, vector=None):
    return (
        build_projection_entry(item, vector_spec,
                               {} if item.tombstone else {
                                   "embedding": vector or [0.1, 0.2, 0.3]}),
        build_projection_entry(item, graph_spec,
                               {} if item.tombstone else {
                                   "nodes": [{"id": item.record_id}], "edges": []}),
    )


def test_manifest_is_reproducible_and_pins_projection_lineage():
    vector_spec, graph_spec = specs()
    first = entries(revision("a"), vector_spec, graph_spec)
    second = entries(revision("b"), vector_spec, graph_spec)
    adapter = WorldviewProjectionAdapter(expected_input_dimension=3)
    manifest = adapter.build_manifest(
        vector_spec=vector_spec, graph_spec=graph_spec,
        vector_entries=[second[0], first[0]], graph_entries=[first[1], second[1]],
        vector_generation=4, graph_generation=7)
    again = adapter.build_manifest(
        vector_spec=vector_spec, graph_spec=graph_spec,
        vector_entries=[first[0], second[0]], graph_entries=[second[1], first[1]],
        vector_generation=4, graph_generation=7)
    assert manifest == again
    assert manifest.record_count == 2
    assert manifest.embedding_model_package_id == "model.embed.v1"
    assert manifest.vector_generation == 4 and manifest.graph_generation == 7


def test_manifest_contains_no_embeddings_graph_payload_or_source_content():
    vector_spec, graph_spec = specs()
    pair = entries(revision(content="private source text"), vector_spec, graph_spec)
    manifest = WorldviewProjectionAdapter(expected_input_dimension=3).build_manifest(
        vector_spec=vector_spec, graph_spec=graph_spec,
        vector_entries=[pair[0]], graph_entries=[pair[1]],
        vector_generation=1, graph_generation=1)
    exposed = repr(manifest.to_dict())
    assert "private source text" not in exposed
    assert "0.1" not in exposed and "nodes" not in exposed


def test_mismatched_record_sets_and_revision_drift_fail_closed():
    vector_spec, graph_spec = specs()
    first = entries(revision("a"), vector_spec, graph_spec)
    other = entries(revision("b"), vector_spec, graph_spec)
    adapter = WorldviewProjectionAdapter(expected_input_dimension=3)
    with pytest.raises(ValueError, match="record sets differ"):
        adapter.build_manifest(vector_spec=vector_spec, graph_spec=graph_spec,
                               vector_entries=[first[0]], graph_entries=[other[1]],
                               vector_generation=1, graph_generation=1)
    changed = create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id=first[0].record_id, content="changed",
        parents=[first[0].revision_id])
    changed_graph = entries(changed, vector_spec, graph_spec)[1]
    with pytest.raises(ValueError, match="revision drift"):
        adapter.build_manifest(vector_spec=vector_spec, graph_spec=graph_spec,
                               vector_entries=[first[0]], graph_entries=[changed_graph],
                               vector_generation=1, graph_generation=2)


def test_dimension_and_spec_roles_are_enforced():
    vector_spec, graph_spec = specs()
    pair = entries(revision(), vector_spec, graph_spec)
    with pytest.raises(ValueError, match="input dimension"):
        WorldviewProjectionAdapter(expected_input_dimension=4).build_manifest(
            vector_spec=vector_spec, graph_spec=graph_spec,
            vector_entries=[pair[0]], graph_entries=[pair[1]],
            vector_generation=1, graph_generation=1)
    with pytest.raises(ValueError, match="one vector and one graph"):
        WorldviewProjectionAdapter(expected_input_dimension=3).build_manifest(
            vector_spec=graph_spec, graph_spec=vector_spec,
            vector_entries=[pair[1]], graph_entries=[pair[0]],
            vector_generation=1, graph_generation=1)


def test_agreed_tombstones_are_excluded_but_retained_as_evidence():
    vector_spec, graph_spec = specs()
    parent = revision()
    deleted = revision(parents=[parent.revision_id], tombstone=True)
    pair = entries(deleted, vector_spec, graph_spec)
    manifest = WorldviewProjectionAdapter(expected_input_dimension=3).build_manifest(
        vector_spec=vector_spec, graph_spec=graph_spec,
        vector_entries=[pair[0]], graph_entries=[pair[1]],
        vector_generation=2, graph_generation=2, allow_empty=True)
    assert manifest.record_count == 0 and manifest.tombstone_count == 1
    assert manifest.record_revisions == ()


def test_duplicate_records_and_cancellation_are_rejected():
    vector_spec, graph_spec = specs()
    pair = entries(revision(), vector_spec, graph_spec)
    adapter = WorldviewProjectionAdapter(expected_input_dimension=3)
    with pytest.raises(ValueError, match="duplicate"):
        adapter.build_manifest(vector_spec=vector_spec, graph_spec=graph_spec,
                               vector_entries=[pair[0], pair[0]],
                               graph_entries=[pair[1]], vector_generation=1,
                               graph_generation=1)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        adapter.build_manifest(vector_spec=vector_spec, graph_spec=graph_spec,
                               vector_entries=[pair[0]], graph_entries=[pair[1]],
                               vector_generation=1, graph_generation=1,
                               cancellation=signal)


def test_forged_projection_evidence_is_rejected_before_manifest_hashing():
    vector_spec, graph_spec = specs()
    pair = entries(revision(), vector_spec, graph_spec)
    forged = ProjectionEntry("proj_" + "0" * 64, pair[0].spec_id,
                             pair[0].record_id, pair[0].revision_id,
                             pair[0].source_content_hash,
                             pair[0].projection_hash, False)
    with pytest.raises(ValueError, match="checksum"):
        WorldviewProjectionAdapter(expected_input_dimension=3).build_manifest(
            vector_spec=vector_spec, graph_spec=graph_spec,
            vector_entries=[forged], graph_entries=[pair[1]],
            vector_generation=1, graph_generation=1)
