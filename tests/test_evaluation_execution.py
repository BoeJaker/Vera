import asyncio

import pytest

from vera.models.evaluation_evidence import (
    CaseEvaluationEvidence, EvaluationCaseIdentity, EvaluationUsage,
    JudgeProvenance, PartialEvaluationReport)
from vera.models.evaluation_execution import (
    EvaluationExecutionPolicy, execute_evaluation, plan_evaluation_execution)
from vera.models.training_contracts import EvaluationRequest, MetricResult, ProviderProfile


pytestmark = pytest.mark.critical
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def request():
    return EvaluationRequest("prompt_package", "ppkg_candidate", "dataset_rev_1", ("score",))


def case(key="case-1"):
    return EvaluationCaseIdentity("dataset_rev_1", key, DIGEST_A, DIGEST_B)


def judge():
    return JudgeProvenance("exact", "1")


def report(profile_id="offline", *, usage=EvaluationUsage()):
    identity = case()
    evidence = CaseEvaluationEvidence(
        identity, "completed", judge(), (MetricResult("score", 1, 0.5),), usage)
    return PartialEvaluationReport(
        request(), profile_id, (identity.case_id,), (evidence,), status="completed")


class Provider:
    def __init__(self, result=None, delay=0):
        self.result = result or report()
        self.delay = delay
        self.calls = 0
        self.cancelled = False

    def profile(self):
        return ProviderProfile(self.result.provider_id, "evaluation", ("offline",))

    async def evaluate(self, evaluation_request):
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_plan_is_payload_free_and_invokes_nothing():
    provider = Provider()
    identity = case()
    plan = plan_evaluation_execution(
        request(), (identity,), provider.profile(), judge(), EvaluationExecutionPolicy())
    assert plan["decision"] == "ready" and plan["effect"] == "none"
    assert plan["provider_invoked"] is False and provider.calls == 0
    assert "input_digest" not in str(plan) and "expected_digest" not in str(plan)


def test_plan_blocks_model_judges_external_providers_and_case_overflow():
    model = JudgeProvenance(
        "judge", "1", kind="model", provider_id="remote",
        model_package_id="mpkg_judge", prompt_package_id="ppkg_judge")
    cases = (case("case-1"), case("case-2"))
    plan = plan_evaluation_execution(
        request(), cases, ProviderProfile("remote", "evaluation", ("judge",)),
        model, EvaluationExecutionPolicy(maximum_cases=1))
    assert plan["decision"] == "blocked"
    assert set(plan["reasons"]) == {
        "case-limit-exceeded", "model-judge-disabled", "external-provider-disabled"}


def test_external_opt_in_still_requires_budget_and_cancellation_contracts():
    profile = ProviderProfile("remote", "evaluation", ("judge",))
    blocked = plan_evaluation_execution(
        request(), (case(),), profile, judge(),
        EvaluationExecutionPolicy(allow_external_providers=True))
    assert set(blocked["reasons"]) == {
        "external-cost-budget-unenforced", "external-cancellation-unverified"}
    ready_profile = ProviderProfile(
        "remote", "evaluation", ("judge", "cost-budget-enforced", "cancellation"))
    ready = plan_evaluation_execution(
        request(), (case(),), ready_profile, judge(),
        EvaluationExecutionPolicy(allow_external_providers=True))
    assert ready["decision"] == "ready"


def test_policy_refuses_unredacted_or_non_vera_cancellation():
    with pytest.raises(ValueError, match="redaction"):
        EvaluationExecutionPolicy(redact_payloads=False)
    with pytest.raises(ValueError, match="own"):
        EvaluationExecutionPolicy(cancellation_owner="provider")
    with pytest.raises(ValueError, match="supported range"):
        EvaluationExecutionPolicy(maximum_cost_microunits=False)


def test_execute_accepts_matching_offline_evidence():
    provider = Provider()
    result = asyncio.run(execute_evaluation(
        provider, request(), (case(),), judge(), EvaluationExecutionPolicy()))
    assert result.passed is True and provider.calls == 1


def test_timeout_cancels_provider_and_returns_failed_payload_free_evidence():
    provider = Provider(delay=0.05)
    result = asyncio.run(execute_evaluation(
        provider, request(), (case(),), judge(),
        EvaluationExecutionPolicy(timeout_ms=1)))
    assert result.status == "failed" and result.error_code == "provider-timeout"
    assert result.cases == () and provider.cancelled is True


def test_cost_overrun_preserves_evidence_but_cannot_pass():
    provider = Provider(report(usage=EvaluationUsage(cost_microunits=2)))
    result = asyncio.run(execute_evaluation(
        provider, request(), (case(),), judge(),
        EvaluationExecutionPolicy(maximum_cost_microunits=1)))
    assert result.status == "failed" and result.error_code == "cost-budget-exceeded"
    assert len(result.cases) == 1 and result.passed is False


@pytest.mark.parametrize("change, message", [
    ("provider", "identity"),
    ("cases", "case set"),
    ("judge", "provenance"),
])
def test_execute_rejects_provider_authority_drift(change, message):
    base = report()
    identity = case()
    if change == "provider":
        provider = Provider(PartialEvaluationReport(
            request(), "other", base.expected_case_ids, base.cases, status="completed"))
        provider.profile = lambda: ProviderProfile("offline", "evaluation", ("offline",))
    elif change == "cases":
        other = case("other")
        provider = Provider(PartialEvaluationReport(
            request(), "offline", (other.case_id,),
            (CaseEvaluationEvidence(other, "completed", judge(),
                                    (MetricResult("score", 1, 0.5),)),),
            status="completed"))
    else:
        alternate = JudgeProvenance("different", "1")
        provider = Provider(PartialEvaluationReport(
            request(), "offline", (identity.case_id,),
            (CaseEvaluationEvidence(identity, "completed", alternate,
                                    (MetricResult("score", 1, 0.5),)),),
            status="completed"))
    with pytest.raises(ValueError, match=message):
        asyncio.run(execute_evaluation(
            provider, request(), (identity,), judge(), EvaluationExecutionPolicy()))
