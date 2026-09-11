"""Payload-free, deterministic evidence contracts for portable evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Mapping, Optional

from .model_package import _identifier
from .training_contracts import (
    EvaluationRequest, LifecycleContractConflict, MetricResult, ProviderProfile, _identity,
    evaluation_request_from_dict)


EVALUATION_EVIDENCE_SCHEMA = "vera.evaluation-evidence/v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(value: str, label: str) -> str:
    value = str(value or "").strip().lower()
    if not _DIGEST.fullmatch(value):
        raise ValueError(f"{label} must be a sha256 digest")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or int(value) != value or int(value) < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return int(value)


@dataclass(frozen=True)
class EvaluationCaseIdentity:
    """Stable case identity without retaining inputs or expected outputs."""

    dataset_revision_id: str
    case_key: str
    input_digest: str
    expected_digest: str
    case_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_revision_id", _identifier(
            self.dataset_revision_id, "dataset revision ID"))
        object.__setattr__(self, "case_key", _identifier(self.case_key, "case key"))
        object.__setattr__(self, "input_digest", _digest(self.input_digest, "input digest"))
        object.__setattr__(self, "expected_digest", _digest(
            self.expected_digest, "expected digest"))
        object.__setattr__(self, "case_id", _identity("ecase_", self.identity_dict()))

    def identity_dict(self) -> dict[str, str]:
        return {
            "dataset_revision_id": self.dataset_revision_id,
            "case_key": self.case_key,
            "input_digest": self.input_digest,
            "expected_digest": self.expected_digest,
        }

    def to_dict(self) -> dict[str, str]:
        return {"case_id": self.case_id, **self.identity_dict()}


@dataclass(frozen=True)
class JudgeProvenance:
    """Exact scorer/judge identity; it never contains prompts or result bodies."""

    scorer_id: str
    scorer_version: str
    kind: str = "deterministic"
    provider_id: str = ""
    model_package_id: str = ""
    prompt_package_id: str = ""
    configuration_digest: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"deterministic", "model", "human"}:
            raise ValueError("judge kind must be deterministic, model or human")
        for name, label in (("scorer_id", "scorer ID"),
                            ("scorer_version", "scorer version")):
            object.__setattr__(self, name, _identifier(getattr(self, name), label))
        for name in ("provider_id", "model_package_id", "prompt_package_id"):
            if getattr(self, name):
                object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.configuration_digest:
            object.__setattr__(self, "configuration_digest", _digest(
                self.configuration_digest, "configuration digest"))
        if self.kind == "model" and not (
                self.provider_id and self.model_package_id and self.prompt_package_id):
            raise ValueError("model judges require provider, model and prompt package IDs")
        if self.kind != "model" and (self.model_package_id or self.prompt_package_id):
            raise ValueError("only model judges may reference model or prompt packages")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class EvaluationUsage:
    """Integer accounting avoids floating-point cost identity drift."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0
    latency_ms: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), name))

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CaseEvaluationEvidence:
    case: EvaluationCaseIdentity
    status: str
    provenance: JudgeProvenance
    metrics: tuple[MetricResult, ...] = ()
    usage: EvaluationUsage = field(default_factory=EvaluationUsage)
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.case, EvaluationCaseIdentity):
            raise TypeError("case must be EvaluationCaseIdentity")
        if not isinstance(self.provenance, JudgeProvenance):
            raise TypeError("provenance must be JudgeProvenance")
        if not isinstance(self.usage, EvaluationUsage):
            raise TypeError("usage must be EvaluationUsage")
        if self.status not in {"completed", "failed", "skipped"}:
            raise ValueError("unsupported case evaluation status")
        metrics = tuple(sorted(self.metrics, key=lambda item: item.metric_id))
        if not all(isinstance(item, MetricResult) for item in metrics):
            raise TypeError("metrics must contain MetricResult values")
        if len({item.metric_id for item in metrics}) != len(metrics):
            raise ValueError("case metric IDs must be unique")
        object.__setattr__(self, "metrics", metrics)
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(
                self.error_code, "error code"))
        if self.status == "completed" and (not metrics or self.error_code):
            raise ValueError("completed cases require metrics and no error code")
        if self.status != "completed" and metrics:
            raise ValueError("unfinished cases cannot claim metrics")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed cases require an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed cases may contain an error code")

    @property
    def passed(self) -> bool:
        return self.status == "completed" and all(item.passed for item in self.metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.to_dict(), "status": self.status,
            "provenance": self.provenance.to_dict(),
            "metrics": [item.to_dict() for item in self.metrics],
            "usage": self.usage.to_dict(), "error_code": self.error_code,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class PartialEvaluationReport:
    """Incremental report whose identity covers expected and observed case IDs."""

    request: EvaluationRequest
    provider_id: str
    expected_case_ids: tuple[str, ...]
    cases: tuple[CaseEvaluationEvidence, ...]
    status: str = "partial"
    error_code: str = ""
    report_id: str = field(init=False)
    schema: str = EVALUATION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request, EvaluationRequest):
            raise TypeError("request must be EvaluationRequest")
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider ID"))
        if self.status not in {"partial", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported evaluation evidence status")
        normalized_expected = tuple(_identifier(item, "case ID")
                                    for item in self.expected_case_ids)
        if len(set(normalized_expected)) != len(normalized_expected):
            raise ValueError("expected case IDs must be unique")
        expected = tuple(sorted(normalized_expected))
        if not expected:
            raise ValueError("expected case IDs cannot be empty")
        object.__setattr__(self, "expected_case_ids", expected)
        cases = tuple(sorted(self.cases, key=lambda item: item.case.case_id))
        if not all(isinstance(item, CaseEvaluationEvidence) for item in cases):
            raise TypeError("cases must contain CaseEvaluationEvidence values")
        observed = tuple(item.case.case_id for item in cases)
        if len(set(observed)) != len(observed):
            raise ValueError("observed case IDs must be unique")
        if not set(observed) <= set(expected):
            raise ValueError("observed cases must belong to the expected case set")
        object.__setattr__(self, "cases", cases)
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(
                self.error_code, "error code"))
        if self.status == "completed" and (set(observed) != set(expected) or self.error_code):
            raise ValueError("completed reports require every expected case and no error")
        if self.status == "partial" and (not cases or len(cases) >= len(expected)):
            raise ValueError("partial reports require a non-empty proper subset of cases")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed reports require an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed reports may contain an error code")
        object.__setattr__(self, "report_id", _identity("eeval_", self.identity_dict()))

    @property
    def coverage(self) -> float:
        return len(self.cases) / len(self.expected_case_ids)

    @property
    def passed(self) -> bool:
        return self.status == "completed" and all(case.passed for case in self.cases)

    @property
    def usage(self) -> EvaluationUsage:
        return EvaluationUsage(
            input_tokens=sum(item.usage.input_tokens for item in self.cases),
            output_tokens=sum(item.usage.output_tokens for item in self.cases),
            cost_microunits=sum(item.usage.cost_microunits for item in self.cases),
            latency_ms=sum(item.usage.latency_ms for item in self.cases),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request": self.request.to_dict(),
            "provider_id": self.provider_id, "status": self.status,
            "expected_case_ids": list(self.expected_case_ids),
            "cases": [item.to_dict() for item in self.cases],
            "error_code": self.error_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "coverage": self.coverage,
            "passed": self.passed, "usage": self.usage.to_dict(),
            **self.identity_dict(),
        }


@dataclass(frozen=True)
class FrozenEvaluationEvidenceFixture:
    """Pinned case identities and already-observed evidence for offline replay."""

    request: EvaluationRequest
    expected_cases: tuple[EvaluationCaseIdentity, ...]
    observations: tuple[CaseEvaluationEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, EvaluationRequest):
            raise TypeError("request must be EvaluationRequest")
        expected = tuple(sorted(self.expected_cases, key=lambda item: item.case_id))
        if not expected or not all(isinstance(item, EvaluationCaseIdentity) for item in expected):
            raise ValueError("expected cases must contain EvaluationCaseIdentity values")
        expected_ids = tuple(item.case_id for item in expected)
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("expected case identities must be unique")
        if any(item.dataset_revision_id != self.request.dataset_revision_id for item in expected):
            raise ValueError("case dataset revisions must match the evaluation request")
        object.__setattr__(self, "expected_cases", expected)
        observations = tuple(sorted(
            self.observations, key=lambda item: item.case.case_id))
        if not observations or not all(
                isinstance(item, CaseEvaluationEvidence) for item in observations):
            raise ValueError("observations must contain CaseEvaluationEvidence values")
        observed_ids = tuple(item.case.case_id for item in observations)
        if len(set(observed_ids)) != len(observed_ids):
            raise ValueError("observed case identities must be unique")
        if not set(observed_ids) <= set(expected_ids):
            raise ValueError("observations must belong to the expected case set")
        requested_metrics = set(self.request.metric_ids)
        if any(item.status == "completed" and
               {metric.metric_id for metric in item.metrics} != requested_metrics
               for item in observations):
            raise ValueError("completed observations must report every requested metric")
        object.__setattr__(self, "observations", observations)


class DeterministicEvidenceEvalProvider:
    """Replay a frozen evidence fixture without executing scorers or judges."""

    PROVIDER_ID = "deterministic-evidence/v1"

    def __init__(self, fixture: FrozenEvaluationEvidenceFixture):
        if not isinstance(fixture, FrozenEvaluationEvidenceFixture):
            raise TypeError("fixture must be FrozenEvaluationEvidenceFixture")
        self._fixture = fixture

    def profile(self) -> ProviderProfile:
        return ProviderProfile(
            self.PROVIDER_ID, "evaluation",
            ("offline", "partial-reports", "stable-case-identity", "provenance", "cost"))

    async def evaluate(self, request: EvaluationRequest) -> PartialEvaluationReport:
        if not isinstance(request, EvaluationRequest):
            raise TypeError("request must be EvaluationRequest")
        if request.evaluation_request_id != self._fixture.request.evaluation_request_id:
            raise ValueError("evaluation request does not match frozen evidence fixture")
        expected_ids = tuple(item.case_id for item in self._fixture.expected_cases)
        status = ("completed" if len(self._fixture.observations) == len(expected_ids)
                  else "partial")
        return PartialEvaluationReport(
            request=request, provider_id=self.PROVIDER_ID,
            expected_case_ids=expected_ids, cases=self._fixture.observations,
            status=status)


@dataclass(frozen=True)
class EvaluationCIPolicy:
    minimum_coverage: float = 1.0
    maximum_failed_cases: int = 0
    require_deterministic_judges: bool = True
    maximum_cost_microunits: Optional[int] = None
    maximum_latency_ms: Optional[int] = None

    def __post_init__(self) -> None:
        coverage = float(self.minimum_coverage)
        if not math.isfinite(coverage) or not 0 < coverage <= 1:
            raise ValueError("minimum coverage must be in (0, 1]")
        object.__setattr__(self, "minimum_coverage", coverage)
        object.__setattr__(self, "maximum_failed_cases", _nonnegative_int(
            self.maximum_failed_cases, "maximum_failed_cases"))
        for name in ("maximum_cost_microunits", "maximum_latency_ms"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _nonnegative_int(value, name))


def evaluate_ci_policy(report: PartialEvaluationReport,
                       policy: EvaluationCIPolicy) -> dict[str, Any]:
    """Evaluate evidence only; never invokes a provider or activates a subject."""
    if not isinstance(report, PartialEvaluationReport):
        raise TypeError("report must be PartialEvaluationReport")
    if not isinstance(policy, EvaluationCIPolicy):
        raise TypeError("policy must be EvaluationCIPolicy")
    failed_cases = sum(not item.passed for item in report.cases)
    model_judges = sum(item.provenance.kind != "deterministic" for item in report.cases)
    usage = report.usage
    checks = {
        "terminal": report.status == "completed",
        "coverage": report.coverage >= policy.minimum_coverage,
        "failed_cases": failed_cases <= policy.maximum_failed_cases,
        "judge_kind": not policy.require_deterministic_judges or model_judges == 0,
        "cost": (policy.maximum_cost_microunits is None or
                 usage.cost_microunits <= policy.maximum_cost_microunits),
        "latency": (policy.maximum_latency_ms is None or
                    usage.latency_ms <= policy.maximum_latency_ms),
    }
    return {
        "schema": "vera.evaluation-ci-decision/v1",
        "report_id": report.report_id,
        "passed": all(checks.values()), "checks": checks,
        "observed": {
            "coverage": report.coverage, "failed_cases": failed_cases,
            "non_deterministic_judges": model_judges, **usage.to_dict(),
        },
        "effect": "none",
        "activation_changed": False,
    }


def evaluation_case_identity_from_dict(value: Mapping[str, Any]) -> EvaluationCaseIdentity:
    try:
        case = EvaluationCaseIdentity(
            value["dataset_revision_id"], value["case_key"],
            value["input_digest"], value["expected_digest"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed evaluation case identity") from exc
    if value.get("case_id") != case.case_id:
        raise LifecycleContractConflict("evaluation case identity does not match content")
    return case


def judge_provenance_from_dict(value: Mapping[str, Any]) -> JudgeProvenance:
    if not isinstance(value, Mapping):
        raise ValueError("malformed judge provenance")
    try:
        return JudgeProvenance(
            scorer_id=value["scorer_id"], scorer_version=value["scorer_version"],
            kind=value.get("kind", "deterministic"),
            provider_id=value.get("provider_id", ""),
            model_package_id=value.get("model_package_id", ""),
            prompt_package_id=value.get("prompt_package_id", ""),
            configuration_digest=value.get("configuration_digest", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed judge provenance") from exc


def evaluation_usage_from_dict(value: Mapping[str, Any]) -> EvaluationUsage:
    if not isinstance(value, Mapping):
        raise ValueError("malformed evaluation usage")
    try:
        return EvaluationUsage(
            input_tokens=value.get("input_tokens", 0),
            output_tokens=value.get("output_tokens", 0),
            cost_microunits=value.get("cost_microunits", 0),
            latency_ms=value.get("latency_ms", 0))
    except (AttributeError, TypeError) as exc:
        raise ValueError("malformed evaluation usage") from exc


def case_evaluation_evidence_from_dict(value: Mapping[str, Any]) -> CaseEvaluationEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("malformed case evaluation evidence")
    try:
        metrics = tuple(MetricResult(
            metric_id=item["metric_id"], value=item["value"],
            threshold=item["threshold"], direction=item.get("direction", "maximize"))
                        for item in value.get("metrics") or ())
        for raw, metric in zip(value.get("metrics") or (), metrics):
            if raw.get("passed") is not metric.passed:
                raise LifecycleContractConflict("case metric pass flag does not match values")
        evidence = CaseEvaluationEvidence(
            case=evaluation_case_identity_from_dict(value["case"]),
            status=value["status"],
            provenance=judge_provenance_from_dict(value["provenance"]),
            metrics=metrics, usage=evaluation_usage_from_dict(value.get("usage") or {}),
            error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed case evaluation evidence") from exc
    if value.get("passed") is not evidence.passed:
        raise LifecycleContractConflict("case pass flag does not match evidence")
    return evidence


def partial_evaluation_report_from_dict(value: Mapping[str, Any]) -> PartialEvaluationReport:
    if not isinstance(value, Mapping):
        raise ValueError("malformed partial evaluation report")
    if value.get("schema") != EVALUATION_EVIDENCE_SCHEMA:
        raise ValueError("unsupported evaluation evidence schema")
    try:
        report = PartialEvaluationReport(
            request=evaluation_request_from_dict(value["request"]),
            provider_id=value["provider_id"],
            expected_case_ids=tuple(value["expected_case_ids"]),
            cases=tuple(case_evaluation_evidence_from_dict(item)
                        for item in value.get("cases") or ()),
            status=value["status"], error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed partial evaluation report") from exc
    if value.get("report_id") != report.report_id:
        raise LifecycleContractConflict("evaluation evidence identity does not match content")
    if value.get("passed") is not report.passed:
        raise LifecycleContractConflict("evaluation evidence pass flag does not match cases")
    try:
        supplied_coverage = float(value.get("coverage"))
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed evaluation coverage") from exc
    if not math.isclose(supplied_coverage, report.coverage, rel_tol=0, abs_tol=1e-12):
        raise LifecycleContractConflict("evaluation coverage does not match cases")
    if value.get("usage") != report.usage.to_dict():
        raise LifecycleContractConflict("evaluation usage does not match cases")
    return report
