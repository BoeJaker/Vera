import asyncio
import copy
from pathlib import Path

import pytest

from vera import capability_orchestration as orchestration
from vera.capability_policy_eval_core import (
    REQUIRED_GATES, evaluate_policy_corpus, load_policy_corpus,
    validate_policy_corpus,
)


pytestmark = pytest.mark.critical
CORPUS = Path(__file__).resolve().parents[1] / "evaluations" / "policy-boundary-v1.json"


def test_frozen_policy_gate_covers_every_named_w1_05_attack():
    corpus = load_policy_corpus(CORPUS)
    validation = validate_policy_corpus(corpus)
    assert validation["valid"] is True
    assert set(validation["gate_coverage"]) == REQUIRED_GATES
    report = evaluate_policy_corpus(corpus)
    assert report["ok"] is True
    assert report["passed"] == report["total"] == 6
    assert report["failed"] == 0 and report["missing_gates"] == []
    assert report["content_free"] is True
    assert report["executes_capabilities"] is False
    assert report["uses_network"] is False
    assert report["uses_external_secrets"] is False


def test_invalid_corpus_fails_closed_without_running_cases():
    corpus = load_policy_corpus(CORPUS)
    broken = copy.deepcopy(corpus)
    broken["cases"] = broken["cases"][:-1]
    report = evaluate_policy_corpus(broken)
    assert report["ok"] is False and report["cases"] == []
    assert "confused_deputy" in report["missing_gates"]


def test_policy_gate_capability_is_bounded_and_deterministic():
    first = asyncio.run(orchestration.eval_policy_boundary.__wrapped__(detail=True))
    second = asyncio.run(orchestration.eval_policy_boundary.__wrapped__(detail=True))
    assert first == second
    assert first["ok"] is True and first["returned"] == 6
    assert "synthetic-" not in repr(first)
    summary = asyncio.run(orchestration.eval_policy_boundary.__wrapped__(detail=False))
    assert "cases" not in summary
    assert summary["total"] == 6 and summary["passed"] == 6
