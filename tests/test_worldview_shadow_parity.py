import math

import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.projection_provider import (
    EmbeddingSpace, ProjectionSpec, build_projection_entry,
)
from vera.fabric.record_revision import create_record_revision
from vera.worldview.worldview_projection_adapter import WorldviewProjectionAdapter
from vera.worldview.worldview_shadow_parity import compare_legacy_worldview_snapshot


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def fixture(seed="a"):
    revision = create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id="rec_" + seed * 64, content=f"record {seed}")
    vector_spec = ProjectionSpec(
        "vector", "chroma", "vector-v1",
        EmbeddingSpace("model.embed.v1", 3, "utf8-nfc-v1", "cosine"))
    graph_spec = ProjectionSpec("graph", "neo4j", "property-graph-v1")
    vector = build_projection_entry(revision, vector_spec,
                                    {"embedding": [0.1, 0.2, 0.3]})
    graph = build_projection_entry(revision, graph_spec,
                                   {"nodes": [{"id": revision.record_id}],
                                    "edges": []})
    return revision, vector_spec, graph_spec, vector, graph


def manifest(*seeds):
    fixtures = [fixture(seed) for seed in seeds]
    first = fixtures[0]
    return WorldviewProjectionAdapter(expected_input_dimension=3).build_manifest(
        vector_spec=first[1], graph_spec=first[2],
        vector_entries=[item[3] for item in fixtures],
        graph_entries=[item[4] for item in fixtures],
        vector_generation=2, graph_generation=4), fixtures


def legacy(item, embedding=None, **extra):
    revision = item[0]
    value = {"dataset_id": "knowledge", "embedding": embedding or [0.1, 0.2, 0.3],
             "text": "must not leak", "revision_id": revision.revision_id,
             "content_hash": revision.content_hash}
    value.update(extra)
    return value


def test_exact_snapshot_is_ready_and_payload_free():
    expected, items = manifest("a", "b")
    records = {item[0].record_id: legacy(item) for item in items}
    edge = (items[0][0].record_id, items[1][0].record_id, "RELATED_TO")
    report = compare_legacy_worldview_snapshot(expected, records, [edge])
    assert report.ready_for_shadow_comparison
    assert report.matched_records == 2 and report.dangling_edge_count == 0
    exposed = repr(report.to_dict())
    assert "must not leak" not in exposed and "0.1" not in exposed


def test_coverage_missing_revision_and_drift_are_classified():
    expected, items = manifest("a", "b")
    records = {
        items[0][0].record_id: legacy(items[0], revision_id=""),
        "rec_" + "c" * 64: legacy(fixture("c")),
    }
    report = compare_legacy_worldview_snapshot(expected, records, [])
    assert report.missing_record_ids == (items[1][0].record_id,)
    assert report.unexpected_record_ids == ("rec_" + "c" * 64,)
    assert report.missing_revision_evidence_record_ids == (items[0][0].record_id,)
    assert not report.ready_for_shadow_comparison
    drifted = {items[0][0].record_id: legacy(items[0], revision_id="rev_" + "0" * 64),
               items[1][0].record_id: legacy(items[1])}
    assert compare_legacy_worldview_snapshot(
        expected, drifted, []).drifted_revision_record_ids == (
            items[0][0].record_id,)
    hash_drift = {items[0][0].record_id: legacy(
        items[0], content_hash="sha256:" + "0" * 64),
        items[1][0].record_id: legacy(items[1])}
    assert compare_legacy_worldview_snapshot(
        expected, hash_drift, []).drifted_revision_record_ids == (
            items[0][0].record_id,)


@pytest.mark.parametrize("embedding", ([0.1], [0.1, math.nan, 0.3], None))
def test_invalid_legacy_embeddings_are_reported_not_coerced(embedding):
    expected, items = manifest("a")
    record = legacy(items[0], embedding=[0.1, 0.2, 0.3])
    record["embedding"] = embedding
    report = compare_legacy_worldview_snapshot(
        expected, {items[0][0].record_id: record}, [])
    assert report.invalid_embedding_record_ids == (items[0][0].record_id,)


def test_dangling_edges_and_malformed_edges_fail_honestly():
    expected, items = manifest("a")
    record_id = items[0][0].record_id
    records = {record_id: legacy(items[0])}
    report = compare_legacy_worldview_snapshot(
        expected, records, [(record_id, "rec_" + "f" * 64, "RELATED_TO")])
    assert report.dangling_edge_count == 1
    with pytest.raises(ValueError, match="must be"):
        compare_legacy_worldview_snapshot(expected, records, [(record_id,)])


def test_cancellation_and_bounds_fail_closed():
    expected, items = manifest("a")
    records = {items[0][0].record_id: legacy(items[0])}
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        compare_legacy_worldview_snapshot(expected, records, [], cancellation=signal)
    with pytest.raises(ValueError, match="record limit"):
        compare_legacy_worldview_snapshot(expected, records, [], max_records=0)
    with pytest.raises(ValueError, match="non-empty strings"):
        compare_legacy_worldview_snapshot(expected, {1: legacy(items[0])}, [])
