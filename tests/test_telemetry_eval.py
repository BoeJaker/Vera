import asyncio
import copy
from pathlib import Path

import pytest

from vera import capability_orchestration as orchestration
from vera.execution.portable_telemetry import exporter_status
from vera.execution.telemetry_eval_core import (
    REQUIRED_GATES,
    evaluate_telemetry_corpus,
    load_telemetry_corpus,
    validate_telemetry_corpus,
)


pytestmark = pytest.mark.critical
CORPUS = Path(__file__).resolve().parents[1] / "evaluations" / "run-telemetry-v1.json"
_STAT_KEYS = ("attempts", "accepted", "failed", "last_status",
              "last_error_type", "last_duration_ms", "last_span_count")


def _stats():
    value = exporter_status()
    return tuple(value[key] for key in _STAT_KEYS)


def test_frozen_telemetry_gate_covers_every_named_w1_06_boundary():
    corpus = load_telemetry_corpus(CORPUS)
    validation = validate_telemetry_corpus(corpus)
    assert validation["valid"] is True
    assert set(validation["gate_coverage"]) == REQUIRED_GATES
    before = _stats()
    report = asyncio.run(evaluate_telemetry_corpus(corpus))
    assert report["ok"] is True
    assert report["passed"] == report["total"] == 7
    assert report["failed"] == 0 and report["missing_gates"] == []
    assert report["content_free"] is True
    assert report["executes_capabilities"] is False
    assert report["uses_network"] is False
    assert report["uses_external_secrets"] is False
    assert report["measures_wall_clock"] is False
    assert _stats() == before


def test_invalid_or_incomplete_corpus_fails_closed_without_cases():
    corpus = load_telemetry_corpus(CORPUS)
    broken = copy.deepcopy(corpus)
    broken["cases"] = broken["cases"][:-1]
    report = asyncio.run(evaluate_telemetry_corpus(broken))
    assert report["ok"] is False and report["cases"] == []
    assert "default_off" in report["missing_gates"]


def test_changed_golden_expectation_fails_the_named_case():
    corpus = load_telemetry_corpus(CORPUS)
    broken = copy.deepcopy(corpus)
    broken["cases"][0]["expected"]["status"] = "STATUS_CODE_ERROR"
    report = asyncio.run(evaluate_telemetry_corpus(broken))
    assert report["ok"] is False and report["failed"] == 1
    assert report["cases"][0]["reason_codes"] == ["observation_mismatch"]


def test_telemetry_gate_capability_is_bounded_deterministic_and_summary_first():
    first = asyncio.run(orchestration.eval_run_telemetry.__wrapped__(detail=True))
    second = asyncio.run(orchestration.eval_run_telemetry.__wrapped__(detail=True))
    assert first == second
    assert first["ok"] is True and first["returned"] == 7
    assert "secret-" not in repr(first)
    summary = asyncio.run(orchestration.eval_run_telemetry.__wrapped__(detail=False))
    assert "cases" not in summary
    assert summary["total"] == 7 and summary["passed"] == 7
