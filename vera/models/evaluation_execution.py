"""Fail-closed execution controls for evidence-producing evaluation providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .evaluation_evidence import (
    EvaluationCaseIdentity, JudgeProvenance, PartialEvaluationReport)
from .training_contracts import EvaluationRequest, ProviderProfile


@dataclass(frozen=True)
class EvaluationExecutionPolicy:
    timeout_ms: int = 30_000
    maximum_cases: int = 1_000
    maximum_cost_microunits: int = 0
    allow_model_judges: bool = False
    allow_external_providers: bool = False
    redact_payloads: bool = True
    cancellation_owner: str = "vera"

    def __post_init__(self) -> None:
        for name, maximum in (("timeout_ms", 3_600_000),
                              ("maximum_cases", 100_000),
                              ("maximum_cost_microunits", 10**15)):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or not 0 < int(value) <= maximum:
                if (name == "maximum_cost_microunits" and
                        not isinstance(value, bool) and value == 0):
                    continue
                raise ValueError(f"{name} must be an integer in its supported range")
            object.__setattr__(self, name, int(value))
        if self.redact_payloads is not True:
            raise ValueError("evaluation payload redaction cannot be disabled")
        if self.cancellation_owner != "vera":
            raise ValueError("Vera must own evaluation timeout and cancellation")


@runtime_checkable
class EvidenceEvalProvider(Protocol):
    def profile(self) -> ProviderProfile: ...
    async def evaluate(self, request: EvaluationRequest) -> PartialEvaluationReport: ...


def plan_evaluation_execution(
        request: EvaluationRequest,
        expected_cases: tuple[EvaluationCaseIdentity, ...],
        provider_profile: ProviderProfile,
        judge: JudgeProvenance,
        policy: EvaluationExecutionPolicy) -> dict[str, Any]:
    """Return an effect-free admission decision for one evaluation attempt."""
    if not isinstance(request, EvaluationRequest):
        raise TypeError("request must be EvaluationRequest")
    if not isinstance(provider_profile, ProviderProfile):
        raise TypeError("provider_profile must be ProviderProfile")
    if provider_profile.kind != "evaluation":
        raise ValueError("provider profile must describe evaluation")
    if not isinstance(judge, JudgeProvenance):
        raise TypeError("judge must be JudgeProvenance")
    if not isinstance(policy, EvaluationExecutionPolicy):
        raise TypeError("policy must be EvaluationExecutionPolicy")
    cases = tuple(expected_cases)
    if not cases or not all(isinstance(item, EvaluationCaseIdentity) for item in cases):
        raise ValueError("expected cases must contain EvaluationCaseIdentity values")
    case_ids = tuple(item.case_id for item in cases)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("expected case identities must be unique")
    if any(item.dataset_revision_id != request.dataset_revision_id for item in cases):
        raise ValueError("case dataset revisions must match the evaluation request")
    reasons = []
    if len(cases) > policy.maximum_cases:
        reasons.append("case-limit-exceeded")
    if judge.kind == "model" and not policy.allow_model_judges:
        reasons.append("model-judge-disabled")
    offline = "offline" in provider_profile.capabilities
    if not offline and not policy.allow_external_providers:
        reasons.append("external-provider-disabled")
    if not offline and policy.allow_external_providers:
        if "cost-budget-enforced" not in provider_profile.capabilities:
            reasons.append("external-cost-budget-unenforced")
        if "cancellation" not in provider_profile.capabilities:
            reasons.append("external-cancellation-unverified")
    return {
        "schema": "vera.evaluation-execution-plan/v1",
        "evaluation_request_id": request.evaluation_request_id,
        "provider_id": provider_profile.provider_id,
        "case_ids": sorted(case_ids),
        "judge": judge.to_dict(),
        "policy": {
            "timeout_ms": policy.timeout_ms,
            "maximum_cases": policy.maximum_cases,
            "maximum_cost_microunits": policy.maximum_cost_microunits,
            "allow_model_judges": policy.allow_model_judges,
            "allow_external_providers": policy.allow_external_providers,
            "redact_payloads": True,
            "cancellation_owner": "vera",
        },
        "decision": "ready" if not reasons else "blocked",
        "reasons": reasons,
        "effect": "none",
        "provider_invoked": False,
    }


async def execute_evaluation(
        provider: EvidenceEvalProvider,
        request: EvaluationRequest,
        expected_cases: tuple[EvaluationCaseIdentity, ...],
        judge: JudgeProvenance,
        policy: EvaluationExecutionPolicy) -> PartialEvaluationReport:
    """Run one admitted provider with Vera-owned timeout and evidence checks."""
    if not isinstance(provider, EvidenceEvalProvider):
        raise TypeError("provider must implement EvidenceEvalProvider")
    profile = provider.profile()
    plan = plan_evaluation_execution(request, expected_cases, profile, judge, policy)
    if plan["decision"] != "ready":
        raise PermissionError("evaluation execution blocked: " + ",".join(plan["reasons"]))
    try:
        report = await asyncio.wait_for(
            provider.evaluate(request), timeout=policy.timeout_ms / 1000)
    except asyncio.TimeoutError:
        return PartialEvaluationReport(
            request=request, provider_id=profile.provider_id,
            expected_case_ids=tuple(item.case_id for item in expected_cases), cases=(),
            status="failed", error_code="provider-timeout")
    if not isinstance(report, PartialEvaluationReport):
        raise TypeError("provider returned a non-evidence report")
    if report.request.evaluation_request_id != request.evaluation_request_id:
        raise ValueError("provider report request does not match execution request")
    if report.provider_id != profile.provider_id:
        raise ValueError("provider report identity does not match its profile")
    if set(report.expected_case_ids) != {item.case_id for item in expected_cases}:
        raise ValueError("provider report case set does not match execution plan")
    if any(item.provenance != judge for item in report.cases):
        raise ValueError("provider report judge provenance does not match execution plan")
    if report.usage.cost_microunits > policy.maximum_cost_microunits:
        return PartialEvaluationReport(
            request=request, provider_id=profile.provider_id,
            expected_case_ids=report.expected_case_ids, cases=report.cases,
            status="failed", error_code="cost-budget-exceeded")
    return report
