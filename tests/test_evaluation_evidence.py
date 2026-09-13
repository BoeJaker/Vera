import asyncio
from dataclasses import FrozenInstanceError

import pytest

from vera.models.evaluation_evidence import (
    CaseEvaluationEvidence, DeterministicEvidenceEvalProvider,
    EvaluationCaseIdentity, EvaluationCIPolicy, EvaluationUsage,
    FrozenEvaluationEvidenceFixture, JudgeProvenance, PartialEvaluationReport,
    evaluate_ci_policy, partial_evaluation_report_from_dict)
from vera.models.training_contracts import (
    EvaluationRequest, LifecycleContractConflict, MetricResult)


pytestmark = pytest.mark.critical
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def request():
    return EvaluationRequest("prompt_package", "ppkg_candidate", "dataset_rev_1",
                             ("accuracy",))


def case(key="case-1", input_digest=DIGEST_A, expected_digest=DIGEST_B):
    return EvaluationCaseIdentity("dataset_rev_1", key, input_digest, expected_digest)


def evidence(identity=None, *, value=1.0, provenance=None, usage=None):
    return CaseEvaluationEvidence(
        identity or case(), "completed",
        provenance or JudgeProvenance("exact-match", "1"),
        (MetricResult("accuracy", value, 0.9),),
        usage or EvaluationUsage())


def test_case_identity_is_content_derived_order_stable_and_payload_free():
    first = case()
    second = case()
    changed = case(input_digest="sha256:" + "c" * 64)
    assert first.case_id == second.case_id
    assert changed.case_id != first.case_id
    assert set(first.to_dict()) == {
        "case_id", "dataset_revision_id", "case_key", "input_digest", "expected_digest"}
    with pytest.raises(FrozenInstanceError):
        first.case_key = "changed"
    with pytest.raises(ValueError, match="sha256"):
        case(input_digest="raw input")


def test_model_judge_requires_complete_package_provenance():
    with pytest.raises(ValueError, match="require provider"):
        JudgeProvenance("judge", "1", kind="model", provider_id="provider")
    judge = JudgeProvenance(
        "judge", "1", kind="model", provider_id="provider",
        model_package_id="mpkg_judge", prompt_package_id="ppkg_judge",
        configuration_digest=DIGEST_A)
    assert judge.kind == "model" and judge.configuration_digest == DIGEST_A
    with pytest.raises(ValueError, match="only model judges"):
        JudgeProvenance("human", "1", kind="human", prompt_package_id="ppkg_bad")


def test_case_evidence_fails_closed_on_false_terminal_claims():
    with pytest.raises(ValueError, match="require metrics"):
        CaseEvaluationEvidence(case(), "completed", JudgeProvenance("exact", "1"))
    with pytest.raises(ValueError, match="cannot claim metrics"):
        CaseEvaluationEvidence(
            case(), "failed", JudgeProvenance("exact", "1"),
            (MetricResult("accuracy", 1, 0.9),), error_code="timeout")
    failed = CaseEvaluationEvidence(
        case(), "failed", JudgeProvenance("exact", "1"), error_code="timeout")
    assert failed.passed is False and failed.error_code == "timeout"


def test_partial_report_preserves_progress_without_claiming_completion():
    first, second = case("case-1"), case("case-2")
    report = PartialEvaluationReport(
        request(), "offline", (first.case_id, second.case_id), (evidence(first),))
    assert report.status == "partial" and report.coverage == 0.5
    assert report.passed is False
    assert report.to_dict()["cases"][0]["case"]["input_digest"] == DIGEST_A
    with pytest.raises(ValueError, match="every expected case"):
        PartialEvaluationReport(
            request(), "offline", (first.case_id, second.case_id),
            (evidence(first),), status="completed")
    with pytest.raises(ValueError, match="proper subset"):
        PartialEvaluationReport(
            request(), "offline", (first.case_id,), (evidence(first),), status="partial")
    with pytest.raises(ValueError, match="expected case IDs must be unique"):
        PartialEvaluationReport(
            request(), "offline", (first.case_id, first.case_id), (evidence(first),))


def test_completed_report_aggregates_integer_usage_and_has_stable_identity():
    first, second = case("case-1"), case("case-2")
    cases = (
        evidence(first, usage=EvaluationUsage(10, 2, 20, 30)),
        evidence(second, usage=EvaluationUsage(5, 1, 10, 15)),
    )
    one = PartialEvaluationReport(
        request(), "offline", (second.case_id, first.case_id),
        tuple(reversed(cases)), status="completed")
    two = PartialEvaluationReport(
        request(), "offline", (first.case_id, second.case_id), cases,
        status="completed")
    assert one.report_id == two.report_id and one.passed is True
    assert one.usage == EvaluationUsage(15, 3, 30, 45)
    assert partial_evaluation_report_from_dict(one.to_dict()) == one


@pytest.mark.parametrize("mutate, message", [
    (lambda value: value["cases"][0]["case"].update(case_key="forged"), "case identity"),
    (lambda value: value.update(coverage=0.25), "coverage"),
    (lambda value: value["usage"].update(input_tokens=999), "usage"),
    (lambda value: value.update(passed=False), "pass flag"),
])
def test_strict_reconstruction_rejects_forged_derived_evidence(mutate, message):
    identity = case()
    report = PartialEvaluationReport(
        request(), "offline", (identity.case_id,), (evidence(identity),),
        status="completed")
    value = report.to_dict()
    mutate(value)
    with pytest.raises(LifecycleContractConflict, match=message):
        partial_evaluation_report_from_dict(value)


def test_ci_policy_fails_partial_nondeterministic_cost_and_quality_evidence_closed():
    first, second = case("case-1"), case("case-2")
    partial = PartialEvaluationReport(
        request(), "offline", (first.case_id, second.case_id), (evidence(first),))
    decision = evaluate_ci_policy(partial, EvaluationCIPolicy(minimum_coverage=0.5))
    assert decision["passed"] is False and decision["checks"]["terminal"] is False
    assert decision["effect"] == "none" and decision["activation_changed"] is False

    model = JudgeProvenance(
        "judge", "1", kind="model", provider_id="provider",
        model_package_id="mpkg_judge", prompt_package_id="ppkg_judge")
    completed = PartialEvaluationReport(
        request(), "offline", (first.case_id, second.case_id),
        (evidence(first, provenance=model, usage=EvaluationUsage(cost_microunits=8)),
         evidence(second, value=0.1)), status="completed")
    failed = evaluate_ci_policy(completed, EvaluationCIPolicy(
        maximum_failed_cases=0, maximum_cost_microunits=5))
    assert failed["passed"] is False
    assert failed["checks"]["judge_kind"] is False
    assert failed["checks"]["cost"] is False
    assert failed["checks"]["failed_cases"] is False


def test_ci_policy_can_require_zero_cost_and_zero_latency():
    identity = case()
    report = PartialEvaluationReport(
        request(), "offline", (identity.case_id,),
        (evidence(identity, usage=EvaluationUsage(cost_microunits=1, latency_ms=1)),),
        status="completed")
    decision = evaluate_ci_policy(report, EvaluationCIPolicy(
        maximum_cost_microunits=0, maximum_latency_ms=0))
    assert decision["checks"]["cost"] is False
    assert decision["checks"]["latency"] is False


def test_offline_evidence_provider_emits_partial_then_complete_without_judge_calls():
    first, second = case("case-1"), case("case-2")
    partial_provider = DeterministicEvidenceEvalProvider(FrozenEvaluationEvidenceFixture(
        request(), (second, first), (evidence(first),)))
    assert partial_provider.profile().capabilities == (
        "cost", "offline", "partial-reports", "provenance", "stable-case-identity")
    partial = asyncio.run(partial_provider.evaluate(request()))
    assert partial.status == "partial" and partial.coverage == 0.5

    complete_provider = DeterministicEvidenceEvalProvider(FrozenEvaluationEvidenceFixture(
        request(), (first, second), (evidence(first), evidence(second))))
    complete = asyncio.run(complete_provider.evaluate(request()))
    assert complete.status == "completed" and complete.passed is True
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(complete_provider.evaluate(EvaluationRequest(
            "prompt_package", "ppkg_other", "dataset_rev_1", ("accuracy",))))


def test_frozen_fixture_rejects_dataset_metric_and_membership_drift():
    first = case()
    foreign = EvaluationCaseIdentity("dataset_rev_2", "case-2", DIGEST_A, DIGEST_B)
    with pytest.raises(ValueError, match="dataset revisions"):
        FrozenEvaluationEvidenceFixture(request(), (foreign,), (evidence(foreign),))
    with pytest.raises(ValueError, match="expected case set"):
        FrozenEvaluationEvidenceFixture(request(), (first,), (evidence(case("case-2")),))
    wrong_metric = CaseEvaluationEvidence(
        first, "completed", JudgeProvenance("exact", "1"),
        (MetricResult("latency", 1, 10, "minimize"),))
    with pytest.raises(ValueError, match="every requested metric"):
        FrozenEvaluationEvidenceFixture(request(), (first,), (wrong_metric,))


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_usage_rejects_non_integer_accounting(value):
    with pytest.raises(ValueError, match="non-negative integer"):
        EvaluationUsage(input_tokens=value)
