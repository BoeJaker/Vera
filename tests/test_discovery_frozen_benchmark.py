import copy
from pathlib import Path

import pytest

from vera.discovery_frozen_benchmark import (
    POLICY,
    evaluate_corpus,
    load_corpus,
    validate_corpus,
)


ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "evaluations" / "discovery-cooperation-v1.json"


@pytest.mark.critical
def test_frozen_cooperation_corpus_improves_all_declared_dimensions():
    result = evaluate_corpus(load_corpus(CORPUS))
    assert result["ok"] is True
    assert result["deterministic"] is True
    assert result["expected_match"] is True
    assert result["decision_scope"] == "frozen_fixture_conformance"
    assert result["production_ready"] is False
    assert result["authority_changed"] is False
    assert result["providers_invoked"] is False
    assert result["effects"] == "none"
    comparison = result["comparison"]
    assert comparison["passed"] is True
    assert comparison["blockers"] == []
    native = comparison["summaries"]["native_research"]
    cooperative = comparison["summaries"]["portable_cooperative"]
    assert cooperative["quality"]["ndcg"] > native["quality"]["ndcg"]
    assert cooperative["quality"]["citation_coverage"] > native["quality"]["citation_coverage"]
    assert cooperative["quality"]["answer_support"] > native["quality"]["answer_support"]
    assert cooperative["quality"]["redundancy"] > native["quality"]["redundancy"]
    assert cooperative["latency_ms"]["first_useful_p95"] < native["latency_ms"]["first_useful_p95"]
    assert cooperative["resources"]["gpu_ms"] == 0


def test_evaluation_report_is_payload_free_and_does_not_claim_production_readiness():
    corpus = load_corpus(CORPUS)
    result = evaluate_corpus(corpus)
    encoded = repr(result)
    for case in corpus["cases"]:
        assert case["query"] not in encoded
        for claim in case["required_claims"]:
            assert claim not in encoded
    assert result["production_ready"] is False
    assert result["authority_changed"] is False


def test_policy_and_exact_observation_matrix_fail_closed():
    corpus = load_corpus(CORPUS)
    changed = copy.deepcopy(corpus)
    changed["policy"]["network_calls"] = True
    result = evaluate_corpus(changed)
    assert result["ok"] is False
    assert {row["code"] for row in result["issues"]} >= {"policy.nonexecuting_required"}

    changed = copy.deepcopy(corpus)
    changed["cases"][0]["variants"]["native_research"]["scout_ms"].pop()
    validation = validate_corpus(changed)
    assert validation["ok"] is False
    assert any(row["code"] == "observation.scout_ms_invalid"
               for row in validation["issues"])


def test_replay_is_stable_and_policy_is_strictly_nonexecuting():
    corpus = load_corpus(CORPUS)
    assert corpus["policy"] == POLICY
    assert evaluate_corpus(corpus) == evaluate_corpus(corpus)


def test_evaluator_source_has_no_runtime_provider_or_model_dependency():
    source = (ROOT / "vera" / "discovery_frozen_benchmark.py").read_text().lower()
    for forbidden in ("requests", "httpx", "capability_registry", "ollama",
                      "torch", "tensorflow", "subprocess", "urlopen"):
        assert forbidden not in source
