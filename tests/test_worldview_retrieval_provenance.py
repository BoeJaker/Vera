import json

import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import model_package_from_dict
from vera.worldview.retrieval_provenance import JepaRetrievalProvenance


pytestmark = pytest.mark.critical


def _snapshot():
    records = (
        {"record_id": "r1", "revision_id": "rev-a", "text": "alpha"},
        {"record_id": "r2", "revision_id": "rev-b", "text": "beta"},
    )
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="worldview-corpus",
        created_at="2026-09-24T12:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string", "text": "string"},
        provenance={"source": "canonical-fabric"},
    )
    return snapshot, frozen


def _binding(blob=b"checkpoint"):
    snapshot, records = _snapshot()
    return JepaRetrievalProvenance.create(
        snapshot=snapshot,
        snapshot_records=records,
        checkpoint_blob=blob,
        indexed_record_ids=("r2", "r1"),
        provider_revision="worldview-jepa-v2",
        framework_version="2.8",
        training_run_id="train-1",
    )


def test_binding_is_content_identified_and_round_trips_strictly():
    binding = _binding()
    restored = JepaRetrievalProvenance.from_dict(
        json.loads(json.dumps(binding.to_dict())))

    assert restored == binding
    assert model_package_from_dict(binding.checkpoint.to_dict()) == binding.checkpoint
    assert binding.receipt() == {
        "schema": "vera.jepa-retrieval-provenance/v1",
        "snapshot_id": binding.snapshot.snapshot_id,
        "model_package_id": binding.checkpoint.package_id,
        "provider_revision": "worldview-jepa-v2",
        "record_count": 2,
        "checkpoint_sha256": binding.checkpoint_sha256,
        "authority": "derived_evidence_only",
        "executes": False,
    }


def test_runtime_requires_exact_checkpoint_and_index_membership():
    binding = _binding()
    assert binding.verifies_runtime(
        checkpoint_blob=b"checkpoint", indexed_record_ids=("r1", "r2"))
    assert not binding.verifies_runtime(
        checkpoint_blob=b"changed", indexed_record_ids=("r1", "r2"))
    assert not binding.verifies_runtime(
        checkpoint_blob=b"checkpoint", indexed_record_ids=("r1",))


def test_query_results_project_only_revision_qualified_snapshot_citations():
    binding = _binding()
    citations = binding.citations_for_result({
        "results": [{"id": "r2", "score": 0.9}, {"id": "r1", "score": 0.8}],
    }, limit=2)
    assert [(item.record_id, item.revision_id) for item in citations] == [
        ("r2", "rev-b"), ("r1", "rev-a")]

    with pytest.raises(ValueError, match="outside"):
        binding.citations_for_result({"results": [{"id": "other"}]}, limit=1)
    with pytest.raises(ValueError, match="successful"):
        binding.citations_for_result({"error": "private backend detail"}, limit=1)


def test_binding_rejects_non_reproducible_snapshot_or_partial_index():
    snapshot, records = _snapshot()
    changed = tuple({**row, "text": "changed"} if row["record_id"] == "r1" else row
                    for row in records)
    with pytest.raises(ValueError, match="exact DatasetSnapshot"):
        JepaRetrievalProvenance.create(
            snapshot=snapshot, snapshot_records=changed,
            checkpoint_blob=b"checkpoint", indexed_record_ids=("r1", "r2"),
            provider_revision="v1")
    with pytest.raises(ValueError, match="membership"):
        JepaRetrievalProvenance.create(
            snapshot=snapshot, snapshot_records=records,
            checkpoint_blob=b"checkpoint", indexed_record_ids=("r1",),
            provider_revision="v1")


def test_binding_rejects_duplicate_record_ids_even_with_different_revisions():
    records = (
        {"record_id": "r1", "revision_id": "rev-a"},
        {"record_id": "r1", "revision_id": "rev-b"},
    )
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id="worldview-corpus", created_at="2026-09-24T12:00:00Z",
        records=records,
        schema={"record_id": "string", "revision_id": "string"},
        provenance={"source": "canonical-fabric"})
    with pytest.raises(ValueError, match="record IDs must be unique"):
        JepaRetrievalProvenance.create(
            snapshot=snapshot, snapshot_records=frozen,
            checkpoint_blob=b"checkpoint", indexed_record_ids=("r1",),
            provider_revision="v1")
