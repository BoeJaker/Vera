import copy
from pathlib import Path

from vera.intel.intel_replay_core import evaluate_corpus, load_corpus, validate_corpus


CORPUS = Path(__file__).resolve().parents[1] / "evaluations" / "intel-replay-v1.json"


def test_frozen_intel_replay_is_exact_and_deterministic():
    corpus = load_corpus(CORPUS)
    first = evaluate_corpus(corpus)
    second = evaluate_corpus(corpus)
    assert first == second
    assert first["ok"] is True
    assert first["accuracy"] == 1.0
    assert first["coverage"] == 1.0
    assert first["deterministic"] is True
    assert first["failed_case_ids"] == []


def test_replay_report_does_not_expose_source_payloads():
    result = evaluate_corpus(load_corpus(CORPUS))
    assert "actual" not in result
    assert "expected" not in result
    assert "source" not in result


def test_invalid_policy_fails_closed_without_replay():
    corpus = copy.deepcopy(load_corpus(CORPUS))
    corpus["policy"]["model_calls"] = True
    result = evaluate_corpus(corpus)
    assert result["ok"] is False
    assert result["coverage"] == 0.0
    assert result["deterministic"] is False
    assert {row["code"] for row in result["issues"]} == {"policy.nonexecuting_required"}


def test_validation_rejects_duplicate_case_ids():
    corpus = copy.deepcopy(load_corpus(CORPUS))
    corpus["cases"].append(copy.deepcopy(corpus["cases"][0]))
    result = validate_corpus(corpus)
    assert result["ok"] is False
    assert "case.id_duplicate" in {row["code"] for row in result["issues"]}
