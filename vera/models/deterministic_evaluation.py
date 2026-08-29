"""Offline reference evaluator for provider-neutral lifecycle contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .model_package import _identifier
from .training_contracts import (
    EvaluationReport, EvaluationRequest, MetricResult, ProviderProfile)


@dataclass(frozen=True)
class ScalarMetricPolicy:
    """Threshold policy for one scalar metric."""

    metric_id: str
    threshold: float
    direction: str = "maximize"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", _identifier(self.metric_id, "metric ID"))
        if self.direction not in {"maximize", "minimize"}:
            raise ValueError("metric direction must be maximize or minimize")
        if isinstance(self.threshold, bool):
            raise TypeError("metric threshold must be numeric")
        threshold = float(self.threshold)
        if not math.isfinite(threshold):
            raise ValueError("metric threshold must be finite")
        object.__setattr__(self, "threshold", threshold)

    def accepts(self, value: float) -> bool:
        return value >= self.threshold if self.direction == "maximize" else value <= self.threshold


@dataclass(frozen=True)
class ScalarCaseObservation:
    """Caller-supplied measurements for one immutable evaluation case."""

    case_id: str
    metrics: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "case ID"))
        if not self.metrics or len(self.metrics) > 128:
            raise ValueError("case metrics must contain 1..128 values")
        normalized = []
        for metric_id, raw_value in self.metrics:
            metric_id = _identifier(metric_id, "metric ID")
            if isinstance(raw_value, bool):
                raise TypeError("metric observations must be numeric")
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError("metric observations must be finite")
            normalized.append((metric_id, value))
        if len({metric_id for metric_id, _ in normalized}) != len(normalized):
            raise ValueError("case metric IDs must be unique")
        object.__setattr__(self, "metrics", tuple(sorted(normalized)))

    def metric_map(self) -> dict[str, float]:
        return dict(self.metrics)


@dataclass(frozen=True)
class ScalarEvaluationFixture:
    """Pinned, already-observed inputs consumed by the reference provider."""

    subject_kind: str
    subject_id: str
    dataset_revision_id: str
    policies: tuple[ScalarMetricPolicy, ...]
    cases: tuple[ScalarCaseObservation, ...]

    def __post_init__(self) -> None:
        if self.subject_kind not in {"model_package", "prompt_package", "capability", "run"}:
            raise ValueError("unsupported evaluation subject kind")
        object.__setattr__(self, "subject_id", _identifier(self.subject_id, "subject ID"))
        object.__setattr__(self, "dataset_revision_id", _identifier(
            self.dataset_revision_id, "dataset revision ID"))
        policies = tuple(self.policies)
        if not policies or len(policies) > 128 or not all(
                isinstance(item, ScalarMetricPolicy) for item in policies):
            raise ValueError("policies must contain 1..128 ScalarMetricPolicy values")
        policies = tuple(sorted(policies, key=lambda item: item.metric_id))
        policy_ids = {item.metric_id for item in policies}
        if len(policy_ids) != len(policies):
            raise ValueError("policy metric IDs must be unique")
        object.__setattr__(self, "policies", policies)
        cases = tuple(self.cases)
        if not cases or len(cases) > 100_000 or not all(
                isinstance(item, ScalarCaseObservation) for item in cases):
            raise ValueError("cases must contain 1..100000 ScalarCaseObservation values")
        cases = tuple(sorted(cases, key=lambda item: item.case_id))
        if len({item.case_id for item in cases}) != len(cases):
            raise ValueError("case IDs must be unique")
        if any(set(item.metric_map()) != policy_ids for item in cases):
            raise ValueError("every case must contain exactly the policy metric set")
        object.__setattr__(self, "cases", cases)


class DeterministicScalarEvalProvider:
    """Aggregate pinned scalar observations without calling any model or service."""

    PROVIDER_ID = "deterministic-scalar/v1"

    def __init__(self, fixture: ScalarEvaluationFixture):
        if not isinstance(fixture, ScalarEvaluationFixture):
            raise TypeError("fixture must be ScalarEvaluationFixture")
        self._fixture = fixture

    def profile(self) -> ProviderProfile:
        return ProviderProfile(
            self.PROVIDER_ID, "evaluation",
            ("offline", "scalar-observations", "arithmetic-mean"))

    async def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        if not isinstance(request, EvaluationRequest):
            raise TypeError("request must be EvaluationRequest")
        fixture = self._fixture
        if (request.subject_kind, request.subject_id) != (
                fixture.subject_kind, fixture.subject_id):
            raise ValueError("evaluation request subject does not match fixture")
        if request.dataset_revision_id != fixture.dataset_revision_id:
            raise ValueError("evaluation request dataset revision does not match fixture")
        policies = {item.metric_id: item for item in fixture.policies}
        unknown = set(request.metric_ids) - set(policies)
        if unknown:
            raise ValueError("evaluation request contains unsupported metrics")

        requested = tuple(request.metric_ids)
        case_maps = tuple(item.metric_map() for item in fixture.cases)
        metrics = tuple(
            MetricResult(
                metric_id,
                math.fsum(case[metric_id] for case in case_maps) / len(case_maps),
                policies[metric_id].threshold,
                policies[metric_id].direction,
            )
            for metric_id in requested
        )
        failed_cases = sum(
            not all(policies[metric_id].accepts(case[metric_id]) for metric_id in requested)
            for case in case_maps
        )
        return EvaluationReport(
            request=request,
            provider_id=self.PROVIDER_ID,
            status="completed",
            metrics=metrics,
            evaluated_cases=len(case_maps),
            failed_cases=failed_cases,
        )
