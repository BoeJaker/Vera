"""Provider-neutral, proposal-only prompt optimization contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from .evaluation_evidence import (
    EvaluationCIPolicy, EvaluationUsage, PartialEvaluationReport, evaluate_ci_policy,
    evaluation_usage_from_dict)
from .model_package import _identifier
from .training_contracts import (
    LifecycleContractConflict, PromptPackage, _identity, prompt_package_from_dict)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or int(value) != value or not 0 < int(value) <= maximum:
        raise ValueError(f"{label} must be an integer in its supported range")
    return int(value)


@dataclass(frozen=True)
class OptimizerProfile:
    optimizer_id: str
    version: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "optimizer_id", _identifier(
            self.optimizer_id, "optimizer ID"))
        object.__setattr__(self, "version", _identifier(self.version, "optimizer version"))
        capabilities = tuple(sorted({_identifier(item, "optimizer capability")
                                     for item in self.capabilities}))
        if not capabilities:
            raise ValueError("optimizer capabilities cannot be empty")
        object.__setattr__(self, "capabilities", capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {"optimizer_id": self.optimizer_id, "version": self.version,
                "capabilities": list(self.capabilities)}


@dataclass(frozen=True)
class OptimizationBudget:
    maximum_candidates: int = 8
    maximum_cost_microunits: int = 0
    maximum_duration_ms: int = 60_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "maximum_candidates", _positive_int(
            self.maximum_candidates, "maximum candidates", 1_000))
        if (isinstance(self.maximum_cost_microunits, bool) or
                int(self.maximum_cost_microunits) != self.maximum_cost_microunits or
                int(self.maximum_cost_microunits) < 0):
            raise ValueError("maximum cost must be a non-negative integer")
        object.__setattr__(self, "maximum_cost_microunits",
                           int(self.maximum_cost_microunits))
        object.__setattr__(self, "maximum_duration_ms", _positive_int(
            self.maximum_duration_ms, "maximum duration", 86_400_000))

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class OptimizationRequest:
    source_prompt_package_id: str
    training_dataset_revision_id: str
    heldout_dataset_revision_id: str
    objective_metric_ids: tuple[str, ...]
    budget: OptimizationBudget = field(default_factory=OptimizationBudget)
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("source_prompt_package_id", "training_dataset_revision_id",
                     "heldout_dataset_revision_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if self.training_dataset_revision_id == self.heldout_dataset_revision_id:
            raise ValueError("training and held-out dataset revisions must differ")
        metrics = tuple(sorted({_identifier(item, "objective metric ID")
                                for item in self.objective_metric_ids}))
        if not metrics:
            raise ValueError("optimization requires at least one objective metric")
        object.__setattr__(self, "objective_metric_ids", metrics)
        if not isinstance(self.budget, OptimizationBudget):
            raise TypeError("budget must be OptimizationBudget")
        object.__setattr__(self, "request_id", _identity("oreq_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "source_prompt_package_id": self.source_prompt_package_id,
            "training_dataset_revision_id": self.training_dataset_revision_id,
            "heldout_dataset_revision_id": self.heldout_dataset_revision_id,
            "objective_metric_ids": list(self.objective_metric_ids),
            "budget": self.budget.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, **self.identity_dict()}


@dataclass(frozen=True)
class PromptOptimizationCandidate:
    prompt_package: PromptPackage
    source_prompt_package_id: str
    optimizer_id: str
    optimizer_version: str
    trace_digest: str
    usage: EvaluationUsage = field(default_factory=EvaluationUsage)
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt_package, PromptPackage):
            raise TypeError("prompt_package must be PromptPackage")
        for name in ("source_prompt_package_id", "optimizer_id", "optimizer_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        digest = str(self.trace_digest or "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError("optimizer trace digest must be sha256")
        object.__setattr__(self, "trace_digest", digest)
        if not isinstance(self.usage, EvaluationUsage):
            raise TypeError("usage must be EvaluationUsage")
        if self.prompt_package.prompt_package_id == self.source_prompt_package_id:
            raise ValueError("optimizer candidate must differ from its source prompt")
        object.__setattr__(self, "candidate_id", _identity("ocand_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "prompt_package": self.prompt_package.to_dict(),
            "source_prompt_package_id": self.source_prompt_package_id,
            "optimizer_id": self.optimizer_id,
            "optimizer_version": self.optimizer_version,
            "trace_digest": self.trace_digest, "usage": self.usage.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, **self.identity_dict()}


@dataclass(frozen=True)
class OptimizerProposal:
    request: OptimizationRequest
    profile: OptimizerProfile
    candidates: tuple[PromptOptimizationCandidate, ...]
    status: str = "proposed"
    error_code: str = ""
    proposal_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, OptimizationRequest):
            raise TypeError("request must be OptimizationRequest")
        if not isinstance(self.profile, OptimizerProfile):
            raise TypeError("profile must be OptimizerProfile")
        if self.status not in {"proposed", "partial", "failed", "cancelled"}:
            raise ValueError("unsupported optimizer proposal status")
        candidates = tuple(self.candidates)
        if not all(isinstance(item, PromptOptimizationCandidate) for item in candidates):
            raise TypeError("candidates must contain PromptOptimizationCandidate values")
        candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValueError("optimizer candidate identities must be unique")
        if len(candidates) > self.request.budget.maximum_candidates:
            raise ValueError("optimizer candidate budget exceeded")
        if any(item.source_prompt_package_id != self.request.source_prompt_package_id or
               item.optimizer_id != self.profile.optimizer_id or
               item.optimizer_version != self.profile.version for item in candidates):
            raise ValueError("candidate provenance does not match proposal request/profile")
        if sum(item.usage.cost_microunits for item in candidates) > \
                self.request.budget.maximum_cost_microunits:
            raise ValueError("optimizer cost budget exceeded")
        object.__setattr__(self, "candidates", candidates)
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(self.error_code, "error code"))
        if self.status == "proposed" and (not candidates or self.error_code):
            raise ValueError("proposed optimizer result requires candidates and no error")
        if self.status == "partial" and not candidates:
            raise ValueError("partial optimizer result requires candidates")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed optimizer result requires an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed optimizer results may contain an error code")
        object.__setattr__(self, "proposal_id", _identity("oprop_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {"schema": "vera.optimizer-proposal/v1", "request": self.request.to_dict(),
                "profile": self.profile.to_dict(), "status": self.status,
                "candidates": [item.to_dict() for item in self.candidates],
                "error_code": self.error_code}

    def to_dict(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "effect": "none",
                "activation_changed": False, **self.identity_dict()}


@runtime_checkable
class OptimizerProvider(Protocol):
    def profile(self) -> OptimizerProfile: ...
    async def propose(self, request: OptimizationRequest) -> OptimizerProposal: ...


@dataclass(frozen=True)
class OptimizerSelectionPolicy:
    metric_id: str
    direction: str = "maximize"
    minimum_improvement: float = 0.0
    ci_policy: EvaluationCIPolicy = field(default_factory=EvaluationCIPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", _identifier(self.metric_id, "metric ID"))
        if self.direction not in {"maximize", "minimize"}:
            raise ValueError("selection direction must be maximize or minimize")
        value = float(self.minimum_improvement)
        if not math.isfinite(value) or value < 0:
            raise ValueError("minimum improvement must be finite and non-negative")
        object.__setattr__(self, "minimum_improvement", value)
        if not isinstance(self.ci_policy, EvaluationCIPolicy):
            raise TypeError("ci_policy must be EvaluationCIPolicy")


def _mean_metric(report: PartialEvaluationReport, metric_id: str) -> float:
    values = [metric.value for case in report.cases for metric in case.metrics
              if metric.metric_id == metric_id]
    if len(values) != len(report.cases):
        raise ValueError("evaluation report does not contain the selection metric for every case")
    return math.fsum(values) / len(values)


def select_optimizer_candidate(
        proposal: OptimizerProposal, baseline: PartialEvaluationReport,
        candidate_reports: Mapping[str, PartialEvaluationReport],
        policy: OptimizerSelectionPolicy) -> dict[str, Any]:
    """Recommend one candidate from held-out evidence; never activates it."""
    if not isinstance(proposal, OptimizerProposal):
        raise TypeError("proposal must be OptimizerProposal")
    if not isinstance(baseline, PartialEvaluationReport):
        raise TypeError("baseline must be PartialEvaluationReport")
    if not isinstance(candidate_reports, Mapping):
        raise TypeError("candidate_reports must be a mapping")
    if not isinstance(policy, OptimizerSelectionPolicy):
        raise TypeError("policy must be OptimizerSelectionPolicy")
    if proposal.status != "proposed":
        raise ValueError("only complete optimizer proposals can be selected")
    if policy.metric_id not in proposal.request.objective_metric_ids:
        raise ValueError("selection metric is not an optimization objective")
    if baseline.request.subject_kind != "prompt_package" or \
            baseline.request.subject_id != proposal.request.source_prompt_package_id:
        raise ValueError("baseline evidence does not match the source prompt")
    if baseline.request.dataset_revision_id != proposal.request.heldout_dataset_revision_id:
        raise ValueError("baseline evidence must use the held-out dataset revision")
    if set(baseline.request.metric_ids) != set(proposal.request.objective_metric_ids):
        raise ValueError("baseline evidence metric set does not match optimization request")
    if set(candidate_reports) != {item.candidate_id for item in proposal.candidates}:
        raise ValueError("candidate evidence set must exactly match the proposal")
    baseline_gate = evaluate_ci_policy(baseline, policy.ci_policy)
    if not baseline_gate["passed"]:
        return {"schema": "vera.optimizer-selection/v1", "proposal_id": proposal.proposal_id,
                "decision": "decline", "reason": "baseline-evidence-failed",
                "recommended_candidate_id": "", "effect": "none",
                "activation_changed": False}
    baseline_value = _mean_metric(baseline, policy.metric_id)
    eligible = []
    for candidate in proposal.candidates:
        report = candidate_reports[candidate.candidate_id]
        if not isinstance(report, PartialEvaluationReport):
            raise TypeError("candidate reports must be PartialEvaluationReport values")
        if report.request.subject_kind != "prompt_package" or \
                report.request.subject_id != candidate.prompt_package.prompt_package_id:
            raise ValueError("candidate evidence subject does not match candidate prompt")
        if report.request.dataset_revision_id != proposal.request.heldout_dataset_revision_id:
            raise ValueError("candidate evidence must use the held-out dataset revision")
        if set(report.request.metric_ids) != set(proposal.request.objective_metric_ids):
            raise ValueError("candidate evidence metric set does not match optimization request")
        if not evaluate_ci_policy(report, policy.ci_policy)["passed"]:
            continue
        value = _mean_metric(report, policy.metric_id)
        improvement = (value - baseline_value if policy.direction == "maximize"
                       else baseline_value - value)
        if improvement >= policy.minimum_improvement:
            eligible.append((improvement, value, candidate.candidate_id))
    eligible.sort(key=lambda item: (-item[0], -item[1] if policy.direction == "maximize"
                                    else item[1], item[2]))
    selected = eligible[0] if eligible else None
    return {"schema": "vera.optimizer-selection/v1", "proposal_id": proposal.proposal_id,
            "decision": "recommend" if selected else "decline",
            "reason": "heldout-improvement" if selected else "no-eligible-improvement",
            "recommended_candidate_id": selected[2] if selected else "",
            "baseline_value": baseline_value,
            "improvement": selected[0] if selected else None,
            "effect": "none", "activation_changed": False}


def optimization_budget_from_dict(value: Mapping[str, Any]) -> OptimizationBudget:
    if not isinstance(value, Mapping):
        raise ValueError("malformed optimization budget")
    try:
        return OptimizationBudget(
            maximum_candidates=value["maximum_candidates"],
            maximum_cost_microunits=value["maximum_cost_microunits"],
            maximum_duration_ms=value["maximum_duration_ms"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed optimization budget") from exc


def optimization_request_from_dict(value: Mapping[str, Any]) -> OptimizationRequest:
    if not isinstance(value, Mapping):
        raise ValueError("malformed optimization request")
    try:
        request = OptimizationRequest(
            source_prompt_package_id=value["source_prompt_package_id"],
            training_dataset_revision_id=value["training_dataset_revision_id"],
            heldout_dataset_revision_id=value["heldout_dataset_revision_id"],
            objective_metric_ids=tuple(value["objective_metric_ids"]),
            budget=optimization_budget_from_dict(value["budget"]))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed optimization request") from exc
    if value.get("request_id") != request.request_id:
        raise LifecycleContractConflict("optimization request identity does not match content")
    return request


def optimizer_profile_from_dict(value: Mapping[str, Any]) -> OptimizerProfile:
    if not isinstance(value, Mapping):
        raise ValueError("malformed optimizer profile")
    try:
        return OptimizerProfile(
            value["optimizer_id"], value["version"], tuple(value["capabilities"]))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed optimizer profile") from exc


def prompt_optimization_candidate_from_dict(
        value: Mapping[str, Any]) -> PromptOptimizationCandidate:
    if not isinstance(value, Mapping):
        raise ValueError("malformed optimizer candidate")
    try:
        candidate = PromptOptimizationCandidate(
            prompt_package=prompt_package_from_dict(value["prompt_package"]),
            source_prompt_package_id=value["source_prompt_package_id"],
            optimizer_id=value["optimizer_id"],
            optimizer_version=value["optimizer_version"],
            trace_digest=value["trace_digest"],
            usage=evaluation_usage_from_dict(value["usage"]))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed optimizer candidate") from exc
    if value.get("candidate_id") != candidate.candidate_id:
        raise LifecycleContractConflict("optimizer candidate identity does not match content")
    return candidate


def optimizer_proposal_from_dict(value: Mapping[str, Any]) -> OptimizerProposal:
    if not isinstance(value, Mapping) or value.get("schema") != "vera.optimizer-proposal/v1":
        raise ValueError("unsupported optimizer proposal schema")
    try:
        proposal = OptimizerProposal(
            request=optimization_request_from_dict(value["request"]),
            profile=optimizer_profile_from_dict(value["profile"]),
            candidates=tuple(prompt_optimization_candidate_from_dict(item)
                             for item in value.get("candidates") or ()),
            status=value["status"], error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed optimizer proposal") from exc
    if value.get("proposal_id") != proposal.proposal_id:
        raise LifecycleContractConflict("optimizer proposal identity does not match content")
    if value.get("effect") != "none" or value.get("activation_changed") is not False:
        raise LifecycleContractConflict("optimizer proposal cannot claim activation effects")
    return proposal
