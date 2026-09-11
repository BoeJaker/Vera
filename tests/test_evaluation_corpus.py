import asyncio
import copy
import json
from pathlib import Path

import pytest

from vera import capability_orchestration as orchestration
from vera.evaluation_corpus_core import (
    canonical_fingerprint, evaluate_resolver_corpus, load_corpus, score_case,
    validate_corpus, validate_resolver_corpus,
)


pytestmark = pytest.mark.critical
CORPUS = Path(__file__).parents[1] / "evaluations" / "frozen-corpus-v1.json"
RESOLVER_CORPUS = Path(__file__).parents[1] / "evaluations" / "resolver-shadow-v1.json"


def test_frozen_corpus_is_valid_and_covers_required_domains():
    report = validate_corpus(load_corpus(CORPUS))
    assert report["ok"] is True
    assert report["lanes"] == {"deterministic": 7, "queued_live": 4}
    assert {"run", "workflow", "tool-selection", "memory", "fabric",
            "generation", "operator", "research", "load"} <= set(report["domains"])


def test_fingerprint_is_order_stable_and_excludes_embedded_fingerprint():
    corpus = load_corpus(CORPUS)
    reordered = json.loads(json.dumps(corpus, sort_keys=True))
    reordered["fingerprint"] = "stale-derived-value"
    assert canonical_fingerprint(corpus) == canonical_fingerprint(reordered)


def test_validation_rejects_duplicate_ids_and_unbudgeted_live_case():
    corpus = load_corpus(CORPUS)
    bad = copy.deepcopy(corpus)
    bad["cases"][1]["id"] = bad["cases"][0]["id"]
    bad["cases"][-1].pop("budget")
    codes = {issue["code"] for issue in validate_corpus(bad)["issues"]}
    assert {"case.id_duplicate", "case.live_budget_required"} <= codes


def test_score_case_is_deterministic_and_fails_missing_paths_closed():
    case = {"id": "resolver", "expected": {
        "authorized": False, "rank.selected": "safe.cap", "executed": False}}
    result = score_case(case, {"authorized": False, "executed": False,
                               "rank": {"selected": "safe.cap"}})
    assert result["ok"] is True and result["score"] == 1.0
    missing = score_case(case, {"authorized": False})
    assert missing["ok"] is False and missing["score"] == pytest.approx(1 / 3, abs=0.0001)


def test_frozen_resolver_corpus_measures_exact_selection_and_unsafe_rate():
    corpus = load_corpus(RESOLVER_CORPUS)
    validation = validate_resolver_corpus(corpus)
    assert validation["ok"] is True and validation["case_count"] == 6
    result = evaluate_resolver_corpus(corpus)
    assert result["ok"] is True
    assert result["selection_accuracy"] == 1.0
    assert result["unsafe_choice_rate"] == 0.0
    assert all(case["authorized"] is False and case["executed"] is False
               for case in result["cases"])


def test_resolver_corpus_validation_rejects_ambiguous_safety_fixtures():
    corpus = load_corpus(RESOLVER_CORPUS)
    corpus["cases"][1]["id"] = corpus["cases"][0]["id"]
    corpus["cases"][2]["unsafe_names"] = ["missing.cap"]
    corpus["cases"][2]["expected"]["selected"] = "missing.cap"
    corpus["cases"][4]["observations"].append(
        dict(corpus["cases"][4]["observations"][0]))
    corpus["policy"]["model_calls"] = True
    codes = {issue["code"] for issue in validate_resolver_corpus(corpus)["issues"]}
    assert {"case.id_duplicate", "case.unsafe_name_unknown",
            "case.expected_selected_unknown", "case.observation_name_duplicate",
            "policy.nonexecuting_required"} <= codes


def test_resolver_evaluator_detects_an_unsafe_choice_and_accuracy_regression():
    corpus = load_corpus(RESOLVER_CORPUS)
    case = corpus["cases"][1]
    case["request"] = {
        "canonical_task": "records.read", "allowed_effects": ["delete"],
        "preferred": ["unsafe.delete"],
    }
    result = evaluate_resolver_corpus(corpus)
    assert result["ok"] is False
    assert result["selection_accuracy"] == pytest.approx(5 / 6, abs=0.0001)
    assert result["unsafe_choice_rate"] == pytest.approx(1 / 6, abs=0.0001)
    unsafe = next(item for item in result["cases"]
                  if item["id"] == "resolver.safe-effect-over-unsafe")
    assert unsafe["selected"] == "unsafe.delete" and unsafe["unsafe_selected"] is True


def test_resolver_evaluation_capability_is_aggregate_by_default_and_bounded_in_detail():
    aggregate = asyncio.run(orchestration.eval_resolver_shadow.__wrapped__())
    assert aggregate["ok"] is True
    assert aggregate["selection_accuracy"] == 1.0
    assert aggregate["unsafe_choice_rate"] == 0.0
    assert "cases" not in aggregate

    detail = asyncio.run(orchestration.eval_resolver_shadow.__wrapped__(
        detail=True, limit=2))
    assert detail["returned"] == 2 and detail["truncated"] is True
    assert len(detail["cases"]) == 2


def test_ontology_decision_capability_is_aggregate_by_default_and_bounded_in_detail():
    aggregate = asyncio.run(orchestration.eval_ontology_decision.__wrapped__(
        timing_repetitions=1))
    assert aggregate["decision"] == "disable_generated_relations"
    assert aggregate["generated_gate"]["passed"] is False
    assert all("cases" not in variant for variant in aggregate["variants"].values())

    detail = asyncio.run(orchestration.eval_ontology_decision.__wrapped__(
        detail=True, limit=2, timing_repetitions=1))
    assert all(variant["returned"] == 2 and variant["truncated"] is True
               for variant in detail["variants"].values())
    assert all(len(variant["cases"]) == 2
               for variant in detail["variants"].values())
