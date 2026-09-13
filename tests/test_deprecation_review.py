import pytest

from vera.inventory.deprecation_inventory import (
    DeprecationCandidate,
    DeprecationInventory,
    SourceCoverage,
    UsageObservation,
    SOURCE_KINDS,
)
from vera.inventory.deprecation_review import (
    IndependentCandidateReview,
    ReviewEvidence,
    digest_evidence,
    generated_ontology_review_evidence,
    inventory_review_evidence,
    review_candidate,
)


pytestmark = pytest.mark.critical


def candidate():
    return DeprecationCandidate(
        name="cap_ontology.generated_relations",
        kind="ontology_projection",
        replacement="cap_ontology.suggest",
        owner="vera.ontologies",
        reason_code="unmeasured_generated_edges",
    )


def inventory(item, *, complete=True, consumers=0):
    coverage = tuple(SourceCoverage(
        source_kind=kind,
        status="complete" if complete else "unavailable",
        snapshot_digest=(digest_evidence({"kind": kind}) if complete else None),
        reason_code=(None if complete else "evidence_not_collected"),
    ) for kind in SOURCE_KINDS)
    observations = () if not consumers else (UsageObservation(
        candidate_id=item.candidate_id,
        source_kind="code_reference",
        classification="consumer",
        source_ref_digest=digest_evidence("consumer.py"),
        count=consumers,
        window_id="review-window-1",
    ),)
    return DeprecationInventory((item,), observations, coverage)


def evidence(item, kind="consumer", assertion="consumer_bound"):
    return ReviewEvidence(item.candidate_id, kind, digest_evidence(kind), assertion)


def test_review_is_deterministic_advisory_and_single_candidate():
    item = candidate()
    inv = inventory(item, complete=False)
    kwargs = dict(
        reviewer="architecture-review", review_window_id="review-window-1",
        recommendation="adapt",
        evidence=(inventory_review_evidence(item, inv),),
        rationale_codes=("evaluation_missing", "generation_default_off"),
        required_actions=("retain_snapshot", "run_quality_evaluation"),
    )
    first = review_candidate(item, inv, **kwargs)
    second = review_candidate(item, inv, **kwargs)
    assert first.review_id == second.review_id
    assert first.to_dict()["removal_authority"] is False
    assert first.to_dict()["executes"] is False
    assert first.to_dict()["mutates"] is False


def test_review_rejects_evidence_for_another_candidate():
    item = candidate()
    other = DeprecationCandidate("other.path", "superseded_path", "new.path",
                                 "vera.other", "superseded")
    with pytest.raises(ValueError, match="exactly one candidate"):
        IndependentCandidateReview(
            item, inventory(item).inventory_id, "reviewer", "window",
            "retain", (evidence(other),), ("still_used",),
            ("retain_surface",), 0, True)


def test_removal_candidacy_fails_closed_without_all_prerequisites():
    item = candidate()
    with pytest.raises(ValueError, match="removal candidacy"):
        review_candidate(
            item, inventory(item, complete=False), reviewer="reviewer",
            review_window_id="window", recommendation="removal_candidate",
            evidence=(inventory_review_evidence(
                item, inventory(item, complete=False)),),
            rationale_codes=("no_consumers",),
            required_actions=("independent_removal_gate",))
    with pytest.raises(ValueError, match="removal candidacy"):
        review_candidate(
            item, inventory(item, consumers=1), reviewer="reviewer",
            review_window_id="window", recommendation="removal_candidate",
            evidence=(inventory_review_evidence(
                item, inventory(item, consumers=1)),),
            rationale_codes=("consumer_present",),
            required_actions=("migrate_consumer",))


def test_removal_candidate_still_has_no_removal_authority():
    item = candidate()
    review = review_candidate(
        item, inventory(item), reviewer="reviewer", review_window_id="window",
        recommendation="removal_candidate",
        evidence=(inventory_review_evidence(item, inventory(item)),
                  evidence(item, "semantic_contract", "replacement_contract_bound"),
                  evidence(item, "rollback", "rollback_verified")),
        rationale_codes=("complete_zero_consumer_inventory",),
        required_actions=("run_independent_removal_gate",))
    assert review.to_dict()["removal_authority"] is False


def test_generated_ontology_evidence_requires_disabled_reversible_state():
    item = candidate()
    snapshot = {"snapshot_id": "capsont_abc", "restorable": True,
                "executes": False, "generated_count": 12}
    generation = {"enabled": False, "rollback": "clear generation flag"}
    consumption = {"enabled": False, "rollback": "clear consumption flag"}
    result = generated_ontology_review_evidence(
        item, snapshot=snapshot, generation_status=generation,
        consumption_status=consumption)
    assert {entry.kind for entry in result} == {
        "snapshot", "runtime_state", "rollback"}
    assert all("generated_count" not in entry.to_dict() for entry in result)
    with pytest.raises(ValueError, match="generation must be disabled"):
        generated_ontology_review_evidence(
            item, snapshot=snapshot,
            generation_status={**generation, "enabled": True},
            consumption_status=consumption)


@pytest.mark.parametrize("bad", ["retain_all", "delete", "retire"])
def test_unknown_recommendations_are_rejected(bad):
    item = candidate()
    with pytest.raises(ValueError, match="recommendation"):
        review_candidate(
            item, inventory(item), reviewer="reviewer", review_window_id="window",
            recommendation=bad,
            evidence=(inventory_review_evidence(item, inventory(item)),),
            rationale_codes=("reason",), required_actions=("action",))


def test_review_rejects_an_inventory_label_not_bound_to_the_inventory():
    item = candidate()
    with pytest.raises(ValueError, match="exact candidate inventory"):
        review_candidate(
            item, inventory(item), reviewer="reviewer", review_window_id="window",
            recommendation="retain",
            evidence=(ReviewEvidence(item.candidate_id, "inventory",
                                     digest_evidence("some-other-inventory"),
                                     "candidate_inventory_bound"),),
            rationale_codes=("still_used",), required_actions=("retain_surface",))
