import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.worldview.evidence_provider import (
    EVIDENCE_KINDS, EvidenceCitation, EvidenceObservation, FrozenEvidenceProvider,
    JepaResultProjector, WorldviewEvidence, evidence_availability,
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


def _project(kind, result, *, support=("record-1",)):
    return JepaResultProjector().project(
        kind=kind, result=result, snapshot=_snapshot(), checkpoint=_checkpoint(),
        provider_revision="runtime-7", observed_at="2026-09-11T10:02:00Z",
        citation_revisions={"record-1": "revision-1", "record-2": "revision-2"},
        support_record_ids=support)


def test_projector_strips_concept_member_text_and_pins_revisions():
    result = {"concepts": [{"idx": 7, "population": 2, "label": "systems",
                            "members_sample": [{"id": "record-1", "text": "private"}]}]}
    evidence = _project("concept", result, support=())
    observation = evidence.to_dict()["observations"][0]
    assert observation["attributes"] == {
        "concept": 7, "label": "systems", "population": 2}
    assert observation["citations"] == [
        {"record_id": "record-1", "revision_id": "revision-1"}]
    assert "private" not in str(evidence.to_dict())


@pytest.mark.parametrize("result", [
    {"next_concepts": [{"concept": 8, "prob": 0.8, "label": "next"}]},
    {"trajectory": [{"step": 0, "concept": 7, "label": "start", "members": ["record-1"]},
                    {"step": 1, "concept": 8, "label": "next", "members": ["record-2"]}]},
])
def test_projector_handles_prediction_and_rollout_shapes(result):
    evidence = _project("prediction", result)
    assert evidence.kind == "prediction"
    assert all(item.citations[0].revision_id == "revision-1"
               for item in evidence.observations)


def test_projector_strips_anomaly_and_reranking_payloads():
    anomaly = _project("anomaly", {"anomalies": [{
        "id": "record-1", "text": "secret source text", "concept": 3,
        "concept_label": "odd", "anomaly_score": 0.91,
        "recon_distance": 0.8, "log_prob": -2.0}]}, support=())
    reranking = _project("reranking", {"query": "private query", "results": [{
        "id": "record-2", "text": "private result", "score": -0.2,
        "concept": 3, "concept_label": "odd"}]}, support=())
    assert anomaly.observations[0].score == 0.91
    assert reranking.observations[0].score == 0.4
    assert "secret source text" not in str(anomaly.to_dict())
    assert "private" not in str(reranking.to_dict())


def test_projector_preserves_counterfactual_distinction_without_causal_claim():
    evidence = _project("counterfactual", {
        "start_concept": 1, "swap_at": 1, "swap_to": 9,
        "baseline": [{"step": 0, "concept": 1, "members": ["record-1"]},
                     {"step": 1, "concept": 2, "members": ["record-2"]}],
        "counterfactual": [{"step": 0, "concept": 1, "members": ["record-1"]},
                           {"step": 1, "concept": 9, "members": ["record-2"]}],
        "divergence_step": 1})
    assert [item.attributes["diverged"] for item in evidence.observations] == [False, True]
    assert evidence.to_dict()["authority"] == "derived_evidence_only"


def test_projector_preserves_drift_measurements_as_derived_evidence():
    evidence = _project("drift", {"dataset_id": "fabric.news", "drifted_concepts": [{
        "concept": 4, "label": "changed", "dataset_frac": 0.4,
        "global_frac": 0.1, "ratio": 4.0, "direction": "over"}]})
    assert evidence.observations[0].attributes == {
        "concept": 4, "dataset_fraction": 0.4, "direction": "over",
        "global_fraction": 0.1, "label": "changed", "ratio": 4.0}


def test_projector_fails_closed_on_errors_missing_citations_and_bad_shapes():
    with pytest.raises(ValueError, match="successful"):
        _project("concept", {"error": "not ready"})
    with pytest.raises(ValueError, match="missing authoritative revision"):
        JepaResultProjector().project(
            kind="anomaly", result={"anomalies": [{"id": "unknown", "anomaly_score": 0.5}]},
            snapshot=_snapshot(), checkpoint=_checkpoint(), provider_revision="runtime-7",
            observed_at="2026-09-11T10:02:00Z", citation_revisions={})
    with pytest.raises(ValueError, match="bounded sequence"):
        _project("prediction", {"next_concepts": None})
    with pytest.raises(ValueError, match="equal length"):
        _project("counterfactual", {"baseline": [{"concept": 1}], "counterfactual": []})
    with pytest.raises(ValueError, match="bounded integer"):
        _project("prediction", {"next_concepts": [{"concept": True, "prob": 0.5}]})
    with pytest.raises(ValueError, match="over or under"):
        _project("drift", {"drifted_concepts": [{
            "concept": 1, "direction": "sideways", "ratio": 1.0}]})
    with pytest.raises(ValueError, match="malformed JEPA concept"):
        _project("concept", {"concepts": [{"population": 1}]})


def test_projector_module_does_not_import_operational_runtime_or_frameworks():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "vera" /
              "worldview" / "evidence_provider.py").read_text(encoding="utf-8")
    assert "worldview_jepa" not in source
    assert "import torch" not in source
    assert "import numpy" not in source
