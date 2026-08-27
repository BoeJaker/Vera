import math

import pytest

from vera.fabric.dataset_provider import CancellationSignal, QueryCancelled
from vera.fabric.projection_provider import (
    EmbeddingSpace, FrozenProjectionProvider, ProjectionEntry, ProjectionSpec,
    build_projection_entry,
)
from vera.fabric.record_revision import create_record_revision


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def revision(seed="a", *, content="hello", parents=(), tombstone=False):
    return create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id="rec_" + seed * 64, content=None if tombstone else content,
        parents=parents, tombstone=tombstone)


def vector_spec(model="model.embed.v1", dimension=3):
    return ProjectionSpec("vector", "qdrant", "vector-v1",
                          EmbeddingSpace(model, dimension, "utf8-nfc-v1", "cosine"))


def vector_entry(item, spec=None, values=None):
    spec = spec or vector_spec()
    return build_projection_entry(item, spec,
                                  {"embedding": values or [0.1, 0.2, 0.3]})


def test_specs_pin_vector_space_and_graph_has_no_embedding_contract():
    first = vector_spec()
    assert first.spec_id == vector_spec().spec_id
    assert first.spec_id != vector_spec(model="model.embed.v2").spec_id
    assert ProjectionSpec("graph", "neo4j", "property-graph-v1").embedding is None
    with pytest.raises(ValueError, match="embedding space"):
        ProjectionSpec("vector", "chroma", "v1")
    with pytest.raises(ValueError, match="embedding space"):
        ProjectionSpec("graph", "neo4j", "v1", first.embedding)


def test_vector_payload_must_match_pinned_dimension_and_be_finite():
    item, spec = revision(), vector_spec()
    with pytest.raises(ValueError, match="dimension"):
        vector_entry(item, spec, [0.1])
    with pytest.raises(ValueError, match="finite"):
        vector_entry(item, spec, [0.1, math.nan, 0.3])


def test_projection_identity_is_stable_across_authoritative_revisions():
    spec = vector_spec()
    first = vector_entry(revision(), spec)
    second_revision = create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id=first.record_id, content="changed", parents=[first.revision_id])
    second = vector_entry(second_revision, spec, [0.3, 0.2, 0.1])
    assert first.projection_id == second.projection_id
    assert first.revision_id != second.revision_id
    assert first.projection_hash != second.projection_hash


def test_apply_is_idempotent_and_updates_are_compare_and_swap():
    spec = vector_spec()
    provider = FrozenProjectionProvider(spec)
    first = vector_entry(revision(), spec)
    assert provider.apply(first) is first
    assert provider.apply(first) is first
    changed_revision = create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id=first.record_id, content="changed", parents=[first.revision_id])
    changed = vector_entry(changed_revision, spec, [0.3, 0.2, 0.1])
    with pytest.raises(ValueError, match="compare-and-swap"):
        provider.apply(changed)
    provider.apply(changed, expected_revision_id=first.revision_id)
    assert provider.generation == 2


def test_reconciliation_classifies_missing_unexpected_and_drift():
    spec = vector_spec()
    provider = FrozenProjectionProvider(spec)
    first = vector_entry(revision("a"), spec)
    unexpected = vector_entry(revision("b"), spec)
    provider.apply(first)
    provider.apply(unexpected)
    drift = vector_entry(revision("a"), spec, [0.3, 0.2, 0.1])
    missing = vector_entry(revision("c"), spec)
    report = provider.reconcile([drift, missing])
    assert report.drifted_projection_ids == (first.projection_id,)
    assert report.missing_projection_ids == (missing.projection_id,)
    assert report.unexpected_projection_ids == (unexpected.projection_id,)
    assert not report.converged


def test_atomic_rebuild_converges_and_generation_is_guarded():
    spec = vector_spec()
    provider = FrozenProjectionProvider(spec)
    provider.apply(vector_entry(revision("a"), spec))
    replacement = [vector_entry(revision("b"), spec)]
    with pytest.raises(ValueError, match="generation"):
        provider.rebuild(replacement, expected_generation=0)
    report = provider.rebuild(replacement, expected_generation=1)
    assert report.converged and report.expected_hash == report.observed_hash
    assert provider.generation == 2


def test_tombstones_have_no_payload_and_cancellation_fails_closed():
    parent = revision("a")
    deleted = create_record_revision(
        namespace="knowledge", record_type="document", created_at=NOW,
        record_id=parent.record_id, parents=[parent.revision_id], tombstone=True)
    spec = vector_spec()
    entry = build_projection_entry(deleted, spec, {})
    assert entry.tombstone
    with pytest.raises(ValueError, match="cannot carry payload"):
        build_projection_entry(deleted, spec, {"embedding": [0.1, 0.2, 0.3]})
    provider = FrozenProjectionProvider(spec)
    provider.apply(entry)
    signal = CancellationSignal()
    signal.cancel()
    with pytest.raises(QueryCancelled):
        provider.snapshot(cancellation=signal)


def test_provider_rejects_forged_projection_identity():
    spec = vector_spec()
    valid = vector_entry(revision(), spec)
    forged = ProjectionEntry("proj_" + "0" * 64, valid.spec_id,
                             valid.record_id, valid.revision_id,
                             valid.source_content_hash, valid.projection_hash, False)
    with pytest.raises(ValueError, match="checksum"):
        FrozenProjectionProvider(spec).apply(forged)
