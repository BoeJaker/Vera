from dataclasses import replace

import pytest

from vera.worldview.lineage_shadow_evaluation import (
    CandidateEvidence, FrozenLineageCase, evaluate_lineage_shadow,
    frozen_lineage_shadow_fixture,
)
from vera.godseye.portable_dataset import make_portable_dataset
from vera.worldview.lineage_dataset_projection import project_lineage_dataset


pytestmark = pytest.mark.critical


def candidate(record_id="j1"):
    return CandidateEvidence(
        lineage="worldview.jepa", record_id=record_id,
        revision_id=f"rev-{record_id}", content_hash="sha256:" + "1" * 64,
    )


def simple_case(candidate_id="worldview.jepa|j1"):
    return FrozenLineageCase(
        case_id="case-1", baseline_ranked=(candidate_id,),
        enriched_ranked=(candidate_id,), relevance=((candidate_id, 3),),
        allowed_lineages=("worldview.jepa",),
    )


def test_frozen_fixture_is_repeatable_bounded_and_explicitly_non_production():
    first = frozen_lineage_shadow_fixture()
    second = frozen_lineage_shadow_fixture()
    assert first == second
    assert first["schema"] == "vera.worldview-lineage-shadow-evaluation/v1"
    assert first["case_count"] == 3
    assert first["candidate_count"] == 7
    assert first["evidence_scope"] == "frozen_fixture_conformance"
    assert first["metrics"]["ndcg_delta_mean"] > 0
    assert first["metrics"]["enriched_recall_mean"] > first["metrics"]["baseline_recall_mean"]
    assert first["metrics"]["contamination_rate_mean"] > 0
    assert first["metrics"]["revision_hash_coverage_mean"] == 1.0
    assert first["decision"] == "insufficient_for_production"
    for key in ("production_ready", "authority_changed",
                "consumer_routing_changed", "models_invoked",
                "providers_invoked", "uses_accelerators", "mutates"):
        assert first[key] is False


def test_unknown_candidates_and_duplicate_evidence_fail_closed():
    with pytest.raises(ValueError, match="unknown candidate"):
        evaluate_lineage_shadow(
            cases=(simple_case("worldview.jepa|missing"),),
            jepa_candidates=(candidate(),), projections=())
    with pytest.raises(ValueError, match="unique"):
        evaluate_lineage_shadow(
            cases=(simple_case(),),
            jepa_candidates=(candidate(), candidate()), projections=())


def test_baseline_cannot_silently_include_cross_lineage_candidates():
    projection = project_lineage_dataset(make_portable_dataset(
        kind="imagery", source="godseye", source_revision="fixture",
        created_at="2026-10-18T00:00:00Z",
        records=[{"id": "g1", "lat": 1, "lng": 2}],
    ))
    cross = "godseye.integrated|g1"
    case = FrozenLineageCase(
        "bad-baseline", (cross,), (cross,), ((cross, 3),),
        ("godseye.integrated",),
    )
    with pytest.raises(ValueError, match="only JEPA"):
        evaluate_lineage_shadow(
            cases=(case,), jepa_candidates=(), projections=(projection,))


def test_invalid_rankings_labels_and_k_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        replace(simple_case(), baseline_ranked=("worldview.jepa|j1",) * 2)
    with pytest.raises(ValueError, match="zero to three"):
        replace(simple_case(), relevance=(("worldview.jepa|j1", 4),))
    with pytest.raises(ValueError, match="one to 100"):
        evaluate_lineage_shadow(
            cases=(simple_case(),), jepa_candidates=(candidate(),),
            projections=(), k=0)


def test_candidate_evidence_requires_hash_and_projection_authority_shape():
    with pytest.raises(ValueError, match="sha256"):
        replace(candidate(), content_hash="not-a-digest")
    with pytest.raises(ValueError, match="do not use lineage projections"):
        replace(candidate(), projection_id="wvlproj_" + "0" * 64)
    with pytest.raises(ValueError, match="require a projection identity"):
        CandidateEvidence(
            lineage="godseye.integrated", record_id="g1",
            revision_id="rev-g1", content_hash="sha256:" + "1" * 64)


def test_forged_projection_identity_fails_closed():
    projection = project_lineage_dataset(make_portable_dataset(
        kind="imagery", source="godseye", source_revision="fixture",
        created_at="2026-10-18T00:00:00Z",
        records=[{"id": "g1", "lat": 1, "lng": 2}],
    ))
    forged = replace(projection, projection_id="wvlproj_" + "0" * 64)
    with pytest.raises(ValueError, match="projection identity"):
        evaluate_lineage_shadow(
            cases=(simple_case(),), jepa_candidates=(candidate(),),
            projections=(forged,))


def test_evaluation_output_contains_no_fixture_payload():
    result = frozen_lineage_shadow_fixture()
    encoded = str(result)
    for forbidden in ("coords", "lat", "lng", "records", "payload", "embedding"):
        assert forbidden not in encoded


def test_evaluator_module_has_no_runtime_or_model_dependency():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "worldview" /
              "lineage_shadow_evaluation.py").read_text(encoding="utf-8")
    for forbidden in ("worldview_jepa import", "godseye_capabilities import",
                      "requests", "httpx", "torch", "tensorflow",
                      "subprocess", "capability_orchestration import"):
        assert forbidden not in source
