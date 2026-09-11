import pytest

from vera.context_provider import ContextCitation, ContextItem
from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.worldview.evidence_provider import EvidenceCitation, EvidenceObservation, WorldviewEvidence
from vera.worldview.reranking_shadow import compare_reranking_shadow


pytestmark = pytest.mark.critical


def _snapshot(label="one"):
    return DatasetSnapshot.create(
        dataset_id="fabric.news", created_at="2026-09-11T10:00:00Z",
        records=[{"id": label}], schema={"type": "object"},
        provenance={"source": "fixture"})[0]


def _checkpoint(version="1"):
    return ModelPackage(
        name="worldview-jepa", version=version, architecture="worldview-jepa-v1",
        format="pytorch",
        artifacts=(ModelArtifact("checkpoint", "artifact://worldview", "a" * 64, 1),),
        compatibility=ModelCompatibility(
            ("worldview.evidence",), "vera.dataset-snapshot/v1",
            "vera.worldview-evidence/v1"))


def _item(record_id, revision, score, *, provider="memory", tokens=2):
    return ContextItem(
        item_id=f"item-{record_id}", text=f"payload {record_id}",
        source=record_id, revision=revision, provider=provider, score=score,
        token_count=tokens,
        citations=(ContextCitation(record_id, f"fabric://{record_id}/{revision}"),))


def _evidence(snapshot, checkpoint, observations):
    return WorldviewEvidence(
        kind="reranking", snapshot=snapshot, checkpoint=checkpoint,
        provider_revision="jepa-runtime-7", observed_at="2026-09-11T10:01:00Z",
        observations=observations)


def _observation(record_id, revision, score, rank=1):
    return EvidenceObservation(
        observation_id=f"reranking:{record_id}", subject_id=f"record:{record_id}",
        score=score, citations=(EvidenceCitation(record_id, revision),),
        attributes={"rank": rank})


def test_exact_identity_evidence_reports_hypothetical_order_without_mutation():
    snapshot, checkpoint = _snapshot(), _checkpoint()
    items = (_item("record-a", "rev-a", .9), _item("record-b", "rev-b", .6))
    evidence = _evidence(snapshot, checkpoint, (
        _observation("record-a", "rev-a", 0, 2),
        _observation("record-b", "rev-b", 1, 1),
    ))
    report = compare_reranking_shadow(
        items, evidence, expected_snapshot_id=snapshot.snapshot_id,
        expected_model_package_id=checkpoint.package_id, weight=.8)
    assert report.status == "compared"
    assert report.matched_candidates == 2
    assert report.changed_positions == 2
    assert [item.item_id for item in items] == ["item-record-a", "item-record-b"]
    assert [item.shadow_rank for item in report.candidates] == [2, 1]
    result = report.to_dict()
    assert result["mode"] == "shadow"
    assert result["authoritative"] is False
    assert result["changes_context_selection"] is False
    assert "payload" not in str(result)


@pytest.mark.parametrize("mismatch,reason", [
    ("snapshot", "dataset_snapshot_mismatch"),
    ("model", "model_package_mismatch"),
])
def test_stale_identity_is_ineligible_and_produces_no_candidate_projection(mismatch, reason):
    snapshot, checkpoint = _snapshot(), _checkpoint()
    evidence = _evidence(snapshot, checkpoint, (_observation("record-a", "rev-a", .8),))
    report = compare_reranking_shadow(
        (_item("record-a", "rev-a", .5),), evidence,
        expected_snapshot_id=("other-snapshot" if mismatch == "snapshot" else snapshot.snapshot_id),
        expected_model_package_id=("other-model" if mismatch == "model" else checkpoint.package_id))
    assert (report.status, report.reason, report.candidates) == ("ineligible", reason, ())


def test_unavailable_evidence_is_explicit_and_does_not_require_candidates():
    report = compare_reranking_shadow(
        (), None, expected_snapshot_id="snapshot-1", expected_model_package_id="model-1")
    assert report.to_dict()["reason"] == "evidence_not_available"
    assert report.to_dict()["candidate_count"] == 0


@pytest.mark.parametrize("observation,match", [
    (_observation("unknown", "rev-a", .5), "outside the candidate set"),
    (EvidenceObservation(
        observation_id="wrong-subject", subject_id="record:other", score=.5,
        citations=(EvidenceCitation("record-a", "rev-a"),)), "subject"),
    (EvidenceObservation(
        observation_id="two-citations", subject_id="record:record-a", score=.5,
        citations=(EvidenceCitation("record-a", "rev-a"),
                   EvidenceCitation("record-b", "rev-b"))), "exactly one"),
])
def test_evidence_cannot_expand_or_ambiguously_claim_candidate_authority(observation, match):
    snapshot, checkpoint = _snapshot(), _checkpoint()
    evidence = _evidence(snapshot, checkpoint, (observation,))
    with pytest.raises(ValueError, match=match):
        compare_reranking_shadow(
            (_item("record-a", "rev-a", .5),), evidence,
            expected_snapshot_id=snapshot.snapshot_id,
            expected_model_package_id=checkpoint.package_id)


def test_duplicate_candidate_authority_and_duplicate_observations_fail_closed():
    snapshot, checkpoint = _snapshot(), _checkpoint()
    observation = _observation("record-a", "rev-a", .5)
    evidence = _evidence(snapshot, checkpoint, (observation,))
    with pytest.raises(ValueError, match="unique"):
        compare_reranking_shadow(
            (_item("record-a", "rev-a", .5),
             _item("record-a", "rev-a", .4, provider="search")), evidence,
            expected_snapshot_id=snapshot.snapshot_id,
            expected_model_package_id=checkpoint.package_id)
    duplicate = _evidence(snapshot, checkpoint, (
        observation,
        EvidenceObservation(
            observation_id="reranking:record-a:again", subject_id="record:record-a",
            score=.7, citations=(EvidenceCitation("record-a", "rev-a"),)),
    ))
    with pytest.raises(ValueError, match="repeats"):
        compare_reranking_shadow(
            (_item("record-a", "rev-a", .5),), duplicate,
            expected_snapshot_id=snapshot.snapshot_id,
            expected_model_package_id=checkpoint.package_id)


def test_shadow_seam_does_not_import_operational_jepa_or_context_registry():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "vera" /
              "worldview" / "reranking_shadow.py").read_text(encoding="utf-8")
    assert "worldview_jepa" not in source
    assert "context_registry" not in source
    assert "import torch" not in source
