import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.worldview.evidence_provider import (
    EVIDENCE_KINDS, EvidenceCitation, EvidenceObservation, FrozenEvidenceProvider,
    WorldviewEvidence, evidence_availability,
)


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
        artifacts=(ModelArtifact(role="checkpoint", uri="artifact://worldview",
                                 sha256="a" * 64, size_bytes=100),),
        compatibility=ModelCompatibility(
            tasks=("worldview.evidence",), input_contract="vera.dataset-snapshot/v1",
            output_contract="vera.worldview-evidence/v1"),
        framework="torch", framework_version="fixture")


def _observation(oid="obs-1"):
    return EvidenceObservation(
        observation_id=oid, subject_id="concept:7", score=0.75,
        citations=(EvidenceCitation("record-1", "revision-1"),),
        attributes={"label": "systems", "rank": 1})


def _evidence(kind="concept", snapshot=None, checkpoint=None, observations=None):
    return WorldviewEvidence(
        kind=kind, snapshot=snapshot or _snapshot(),
        checkpoint=checkpoint or _checkpoint(), provider_revision="jepa-runtime-7",
        observed_at="2026-09-11T10:01:00Z",
        observations=observations if observations is not None else (_observation(),))


@pytest.mark.parametrize("kind", sorted(EVIDENCE_KINDS))
def test_all_roadmap_evidence_kinds_share_versioned_identity(kind):
    evidence = _evidence(kind)
    result = evidence.to_dict()
    assert result["kind"] == kind
    assert result["snapshot_id"] == _snapshot().snapshot_id
    assert result["model_package_id"] == _checkpoint().package_id
    assert result["authority"] == "derived_evidence_only"
    assert result["executes"] is False
    assert result["evidence_id"].startswith("wve_")


def test_identity_is_order_stable_and_changes_with_authority_revisions():
    one, two = _observation("one"), _observation("two")
    assert _evidence(observations=(one, two)).evidence_id == _evidence(
        observations=(two, one)).evidence_id
    assert _evidence(snapshot=_snapshot("two")).evidence_id != _evidence().evidence_id
    assert _evidence(checkpoint=_checkpoint("2")).evidence_id != _evidence().evidence_id


def test_checkpoint_must_be_explicit_jepa_worldview_model_package():
    bad = ModelPackage(
        name="godseye", version="1", architecture="godseye-vision", format="pytorch",
        artifacts=(ModelArtifact("checkpoint", "artifact://godseye", "b" * 64, 1),),
        compatibility=ModelCompatibility(("vision",), "input/v1", "output/v1"))
    with pytest.raises(ValueError, match="JEPA Worldview"):
        _evidence(checkpoint=bad)
    incompatible = ModelPackage(
        name="worldview-jepa", version="1", architecture="worldview-jepa-v1",
        format="pytorch",
        artifacts=(ModelArtifact("checkpoint", "artifact://worldview", "c" * 64, 1),),
        compatibility=ModelCompatibility(("worldview.predict",), "input/v1", "output/v1"))
    with pytest.raises(ValueError, match="not compatible"):
        _evidence(checkpoint=incompatible)


def test_observations_require_citations_and_reject_payload_or_secret_fields():
    with pytest.raises(ValueError, match="citations"):
        EvidenceObservation(observation_id="one", subject_id="concept:1", citations=())
    for key in ("text", "embedding", "prompt", "secret"):
        with pytest.raises(ValueError, match="payload-bearing"):
            EvidenceObservation(
                observation_id="one", subject_id="concept:1",
                citations=(EvidenceCitation("record-1", "revision-1"),),
                attributes={key: "not retained"})
    with pytest.raises(ValueError, match="payload-bearing"):
        EvidenceObservation(
            observation_id="one", subject_id="concept:1",
            citations=(EvidenceCitation("record-1", "revision-1"),),
            attributes={"safe": {"payload": "nested"}})


def test_nonfinite_scores_and_duplicate_identity_fail_closed():
    for score in (float("nan"), float("inf"), -0.1, 1.1, True):
        with pytest.raises(ValueError, match="score"):
            EvidenceObservation(
                observation_id="one", subject_id="concept:1", score=score,
                citations=(EvidenceCitation("record-1", "revision-1"),))
    with pytest.raises(ValueError, match="unique"):
        _evidence(observations=(_observation(), _observation()))


def test_evidence_cannot_predate_snapshot():
    with pytest.raises(ValueError, match="predate"):
        WorldviewEvidence(
            kind="drift", snapshot=_snapshot(), checkpoint=_checkpoint(),
            provider_revision="runtime-1", observed_at="2026-09-11T09:59:59Z",
            observations=())


def test_frozen_provider_filters_without_execution_or_fallback():
    concept = _evidence("concept")
    drift = _evidence("drift")
    provider = FrozenEvidenceProvider((drift, concept, concept))
    assert provider.get(concept.evidence_id) == concept
    assert provider.list(kind="concept") == (concept,)
    assert provider.list(snapshot_id=concept.snapshot_id) == tuple(sorted(
        (concept, drift), key=lambda item: item.evidence_id))
    with pytest.raises(ValueError, match="evidence_id"):
        provider.get("not-an-id")


def test_stale_and_unavailable_evidence_are_never_usable():
    evidence = _evidence()
    assert evidence_availability(None, expected_snapshot_id=evidence.snapshot_id,
                                 expected_model_package_id=evidence.model_package_id) == {
        "status": "unavailable", "usable": False, "reason": "evidence_not_available"}
    assert evidence_availability(
        evidence, expected_snapshot_id="snap_" + "0" * 64,
        expected_model_package_id=evidence.model_package_id)["reason"] == \
        "dataset_snapshot_mismatch"
    assert evidence_availability(
        evidence, expected_snapshot_id=evidence.snapshot_id,
        expected_model_package_id="mpkg_" + "0" * 64)["reason"] == \
        "model_package_mismatch"
    assert evidence_availability(
        evidence, expected_snapshot_id=evidence.snapshot_id,
        expected_model_package_id=evidence.model_package_id)["usable"] is True
