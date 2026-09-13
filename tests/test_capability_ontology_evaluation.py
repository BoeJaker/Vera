from pathlib import Path

import pytest

from vera.ontologies.capability_ontology_evaluation import (
    evaluate_ontology_decision_corpus,
    load_ontology_decision_corpus,
    validate_ontology_decision_corpus,
)


pytestmark = pytest.mark.critical
FIXTURE = Path(__file__).resolve().parents[1] / "evaluations" / "capability-ontology-decision-v1.json"


def test_frozen_corpus_is_non_authoritative_and_generated_relations_fail_gate():
    corpus = load_ontology_decision_corpus(FIXTURE)
    report = evaluate_ontology_decision_corpus(corpus, timing_repetitions=2)
    assert report["ok"] is True
    assert report["mode"] == "shadow"
    assert report["authoritative"] is False
    assert report["model_calls"] is False
    assert report["capability_execution"] is False
    assert report["variants"]["curated"]["selection_accuracy"] > report["variants"]["baseline"]["selection_accuracy"]
    assert report["variants"]["generated"]["selection_accuracy"] < report["variants"]["curated"]["selection_accuracy"]
    assert report["variants"]["generated"]["invalid_relation_cases"] == 1
    assert report["generated_gate"]["passed"] is False
    assert report["decision"] == "disable_generated_relations"


def test_resolver_policy_excludes_unsafe_preference_and_caller_preference_wins():
    report = evaluate_ontology_decision_corpus(
        load_ontology_decision_corpus(FIXTURE), timing_repetitions=1)
    for variant in ("baseline", "curated", "generated"):
        cases = {case["id"]: case for case in report["variants"][variant]["cases"]}
        assert cases["effects-remain-authoritative"]["selected"] == "records.read"
        assert cases["effects-remain-authoritative"]["unsafe_selected"] is False
        assert cases["caller-preference-remains-authoritative"]["selected"] == "search.remote"
    assert report["variants"]["curated"]["cases"][3]["hint_status"] == "caller_preference_authoritative"


def test_report_covers_ambiguity_tokens_latency_and_relation_precision():
    report = evaluate_ontology_decision_corpus(
        load_ontology_decision_corpus(FIXTURE), timing_repetitions=3)
    for variant in report["variants"].values():
        assert 0 <= variant["ambiguity_rate"] <= 1
        assert variant["hint_estimated_tokens"] >= 0
        assert variant["latency_us"]["status"] == "observed_local"
        assert variant["latency_us"]["repetitions"] == 3
        assert variant["latency_us"]["p95"] >= 0
    assert report["variants"]["curated"]["relation_precision"] == 1.0


def test_cyclic_generated_edges_fail_closed_to_baseline_request():
    report = evaluate_ontology_decision_corpus(
        load_ontology_decision_corpus(FIXTURE), timing_repetitions=1)
    generated = {case["id"]: case for case in report["variants"]["generated"]["cases"]}
    case = generated["generated-cycle-fails-closed"]
    assert case["hint_status"] == "invalid"
    assert "cycle" in case["invalid_reason"]
    assert case["selected"] == "image.alpha"
    assert case["applied_relations"] == 0


@pytest.mark.parametrize("mutation,code", [
    (lambda corpus: corpus.update(schema="wrong"), "schema.invalid"),
    (lambda corpus: corpus["policy"].update(ontology_activation=True),
     "policy.non_authoritative_required"),
    (lambda corpus: corpus["cases"].append(corpus["cases"][0]), "case.id_invalid"),
])
def test_invalid_corpus_fails_before_comparison(mutation, code):
    corpus = load_ontology_decision_corpus(FIXTURE)
    mutation(corpus)
    validation = validate_ontology_decision_corpus(corpus)
    assert validation["ok"] is False
    assert code in {issue["code"] for issue in validation["issues"]}
    assert evaluate_ontology_decision_corpus(corpus)["decision"] == "invalid_corpus"


def test_timing_repetitions_are_bounded():
    corpus = load_ontology_decision_corpus(FIXTURE)
    with pytest.raises(ValueError, match="between 1 and 200"):
        evaluate_ontology_decision_corpus(corpus, timing_repetitions=0)
