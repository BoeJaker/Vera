from dataclasses import replace

import pytest

from vera.minds.evidence_recovery_evaluation import (
    FrozenEvidenceAttempt, FrozenRecoveryCase, evaluate_frozen_recovery,
    frozen_recovery_fixture,
)


pytestmark = pytest.mark.critical


def test_frozen_fixture_is_repeatable_and_explicitly_non_production():
    first = frozen_recovery_fixture()
    second = frozen_recovery_fixture()
    assert first == second
    assert first["schema"] == "vera.four-minds-recovery-evaluation/v1"
    assert first["case_count"] == 2
    assert first["evidence_scope"] == "frozen_fixture_conformance"
    assert first["recovery_scope"] == "evidence_reconstruction_only"
    assert first["rollback_scope"] == "route_identity_rehearsal_only"
    assert first["metrics"] == {
        "regression_detection_rate": 1.0,
        "evidence_recovery_rate": 1.0,
        "rollback_identity_rate": 1.0,
        "rollback_completion_rate": 1.0,
    }
    assert first["decision"] == "insufficient_for_production"
    for field in (
            "production_ready", "routes_executed", "rollback_executed",
            "models_invoked", "providers_invoked", "uses_accelerators",
            "writes", "authority_changed"):
        assert first[field] is False


def test_fixture_detects_both_answer_and_missing_evidence_regressions():
    result = frozen_recovery_fixture()
    rows = {row["case_id"]: row for row in result["cases"]}
    assert rows["knn-index-regression"]["failure_class"] == (
        "missing_evidence_and_answer_regression_and_outcome_regression")
    assert rows["model-package-regression"]["failure_class"] == "missing_evidence"
    for row in rows.values():
        candidate = next(item for item in row["attempts"]
                         if item["stage"] == "candidate")
        assert candidate["eligible_for_evaluation"] is False
        assert candidate["missing"]


def test_rollback_must_restore_the_exact_baseline_route_identity():
    from vera.minds.evidence_recovery_evaluation import _attempt

    route = {
        "kind": "nlp_knn", "node_id": "cpu-246", "index_id": "index-1",
        "index_version": "v1", "route_revision": "stable",
    }
    make = lambda identity, selected=route: _attempt(
        identity, "accept", selected, implementation="knn:v1",
        node="cpu-246", package="student.knn")
    case = FrozenRecoveryCase(
        "rollback-mismatch", "accept", "observed", make("baseline"),
        make("candidate"), make("recovery"),
        make("rollback", {**route, "index_version": "v2"}))
    result = evaluate_frozen_recovery((case,))
    assert result["cases"][0]["rollback_identity_restored"] is False
    assert result["cases"][0]["rollback_complete"] is False
    assert result["production_ready"] is False
    assert result["corpus_id"] != frozen_recovery_fixture()["corpus_id"]


def test_invalid_or_contaminated_cases_fail_closed():
    from vera.minds.evidence_recovery_evaluation import _attempt

    route = {
        "kind": "nlp_knn", "node_id": "cpu-246", "index_id": "index-1",
        "index_version": "v1",
    }
    make = lambda identity: _attempt(
        identity, "accept", route, implementation="knn:v1",
        node="cpu-246", package="student.knn")
    case = FrozenRecoveryCase(
        "case-1", "accept", "observed", make("base"), make("candidate"),
        make("recovery"), make("rollback"))
    with pytest.raises(ValueError, match="unique frozen recovery cases"):
        evaluate_frozen_recovery((case, replace(case)))
    contaminated = dict(case.recovery.decision)
    contaminated["state_hash"] = "different-state"
    with pytest.raises(ValueError, match="preserve question and state identity"):
        replace(case, recovery=replace(case.recovery, decision=contaminated))
    with pytest.raises(ValueError, match="baseline must be complete"):
        evaluate_frozen_recovery((replace(case, baseline=replace(
            case.baseline,
            route={"kind": "nlp_knn", "node_id": "cpu-246"})),))


def test_result_contains_only_payload_free_evidence():
    rendered = repr(frozen_recovery_fixture())
    for forbidden in (
            "frozen-state", "state_ref", "labels", "detail", "'accept'",
            "'revise'", "payload", "prompt", "embedding"):
        assert forbidden not in rendered


def test_evaluator_has_no_runtime_or_model_dependency():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "minds" /
              "evidence_recovery_evaluation.py").read_text(encoding="utf-8")
    for forbidden in (
            "capability_orchestration", "CAPABILITY_REGISTRY", "requests",
            "httpx", "redis", "asyncpg", "sqlite3", "subprocess", "torch",
            "tensorflow", "numpy"):
        assert forbidden not in source
