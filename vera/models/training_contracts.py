"""Provider-neutral, non-executing training, evaluation and prompt contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping, Protocol, runtime_checkable

from .model_package import _identifier, _string


PROMPT_SCHEMA = "vera.prompt-package/v1"
TRAINING_SCHEMA = "vera.training-run/v1"
EVALUATION_SCHEMA = "vera.evaluation-report/v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _pairs(value: Mapping[str, Any] | tuple[tuple[str, Any], ...], field_name: str,
           *, limit: int = 128) -> tuple[tuple[str, Any], ...]:
    items = tuple(value.items()) if isinstance(value, Mapping) else tuple(value)
    if len(items) > limit:
        raise ValueError(f"{field_name} has too many entries")
    out = []
    for key, item in items:
        key = _identifier(key, f"{field_name} key")
        if isinstance(item, bool) or item is None:
            normalized = item
        elif isinstance(item, int):
            normalized = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{field_name} values must be finite")
            normalized = item
        elif isinstance(item, str):
            normalized = _string(item, f"{field_name} value", limit=2048)
        else:
            raise TypeError(f"{field_name} values must be JSON scalars")
        out.append((key, normalized))
    if len({key for key, _ in out}) != len(out):
        raise ValueError(f"{field_name} keys must be unique")
    return tuple(sorted(out))


@dataclass(frozen=True)
class PromptMessage:
    role: str
    template: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported prompt role")
        object.__setattr__(self, "template", _string(
            self.template, "prompt template", required=True, limit=100_000))

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class PromptPackage:
    name: str
    version: str
    messages: tuple[PromptMessage, ...]
    variables: tuple[str, ...] = ()
    output_contract: str = "text/v1"
    metadata: tuple[tuple[str, Any], ...] = ()
    prompt_package_id: str = field(init=False)
    schema: str = PROMPT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "prompt name"))
        object.__setattr__(self, "version", _identifier(self.version, "prompt version"))
        messages = tuple(self.messages)
        if not messages or len(messages) > 64 or not all(
                isinstance(item, PromptMessage) for item in messages):
            raise ValueError("messages must contain 1..64 PromptMessage values")
        object.__setattr__(self, "messages", messages)
        variables = tuple(sorted({_identifier(item, "prompt variable")
                                  for item in self.variables}))
        object.__setattr__(self, "variables", variables)
        object.__setattr__(self, "output_contract", _identifier(
            self.output_contract, "output contract"))
        object.__setattr__(self, "metadata", _pairs(self.metadata, "prompt metadata"))
        object.__setattr__(self, "prompt_package_id", _identity(
            "ppkg_", self.identity_dict()))

    def identity_dict(self) -> dict:
        return {"schema": self.schema, "name": self.name, "version": self.version,
                "messages": [item.to_dict() for item in self.messages],
                "variables": list(self.variables), "output_contract": self.output_contract,
                "metadata": dict(self.metadata)}

    def to_dict(self) -> dict:
        return {"prompt_package_id": self.prompt_package_id, **self.identity_dict()}


@dataclass(frozen=True)
class TrainingRequest:
    dataset_revision_id: str
    objective: str
    base_model_package_id: str = ""
    prompt_package_id: str = ""
    hyperparameters: tuple[tuple[str, Any], ...] = ()
    training_request_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_revision_id", _identifier(
            self.dataset_revision_id, "dataset revision ID"))
        object.__setattr__(self, "objective", _identifier(self.objective, "objective"))
        for name in ("base_model_package_id", "prompt_package_id"):
            if getattr(self, name):
                object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "hyperparameters", _pairs(
            self.hyperparameters, "hyperparameters"))
        object.__setattr__(self, "training_request_id", _identity(
            "treq_", self.identity_dict()))

    def identity_dict(self) -> dict:
        return {"dataset_revision_id": self.dataset_revision_id,
                "objective": self.objective,
                "base_model_package_id": self.base_model_package_id,
                "prompt_package_id": self.prompt_package_id,
                "hyperparameters": dict(self.hyperparameters)}

    def to_dict(self) -> dict:
        return {"training_request_id": self.training_request_id, **self.identity_dict()}


@dataclass(frozen=True)
class TrainingRun:
    training_run_id: str
    request: TrainingRequest
    runtime_id: str
    status: str
    attempts: int = 0
    output_model_package_id: str = ""
    evaluation_report_ids: tuple[str, ...] = ()
    error_code: str = ""
    schema: str = TRAINING_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "training_run_id", _identifier(
            self.training_run_id, "training run ID"))
        if not isinstance(self.request, TrainingRequest):
            raise TypeError("request must be TrainingRequest")
        object.__setattr__(self, "runtime_id", _identifier(self.runtime_id, "runtime ID"))
        if self.status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError("unsupported training status")
        if isinstance(self.attempts, bool) or int(self.attempts) < 0:
            raise ValueError("attempts must be non-negative")
        object.__setattr__(self, "attempts", int(self.attempts))
        for name in ("output_model_package_id", "error_code"):
            if getattr(self, name):
                object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "evaluation_report_ids", tuple(sorted({
            _identifier(item, "evaluation report ID")
            for item in self.evaluation_report_ids})))
        if self.status == "succeeded" and not self.output_model_package_id:
            raise ValueError("succeeded training requires an output ModelPackage")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed training requires an error code")
        if self.status in {"queued", "running"} and (
                self.output_model_package_id or self.error_code or self.evaluation_report_ids):
            raise ValueError("non-terminal training cannot contain terminal results")
        if self.status in {"failed", "cancelled"} and (
                self.output_model_package_id or self.evaluation_report_ids):
            raise ValueError("unsuccessful training cannot claim output results")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed training can contain an error code")

    def to_dict(self) -> dict:
        return {"schema": self.schema, "training_run_id": self.training_run_id,
                "request": self.request.to_dict(), "runtime_id": self.runtime_id,
                "status": self.status, "attempts": self.attempts,
                "output_model_package_id": self.output_model_package_id,
                "evaluation_report_ids": list(self.evaluation_report_ids),
                "error_code": self.error_code}


@dataclass(frozen=True)
class EvaluationRequest:
    subject_kind: str
    subject_id: str
    dataset_revision_id: str
    metric_ids: tuple[str, ...]
    prompt_package_id: str = ""
    evaluation_request_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.subject_kind not in {"model_package", "prompt_package", "capability", "run"}:
            raise ValueError("unsupported evaluation subject kind")
        for name in ("subject_id", "dataset_revision_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        metrics = tuple(sorted({_identifier(item, "metric ID") for item in self.metric_ids}))
        if not metrics:
            raise ValueError("evaluation requires at least one metric")
        object.__setattr__(self, "metric_ids", metrics)
        if self.prompt_package_id:
            object.__setattr__(self, "prompt_package_id", _identifier(
                self.prompt_package_id, "prompt package ID"))
        object.__setattr__(self, "evaluation_request_id", _identity(
            "ereq_", self.identity_dict()))

    def identity_dict(self) -> dict:
        return {"subject_kind": self.subject_kind, "subject_id": self.subject_id,
                "dataset_revision_id": self.dataset_revision_id,
                "metric_ids": list(self.metric_ids),
                "prompt_package_id": self.prompt_package_id}

    def to_dict(self) -> dict:
        return {"evaluation_request_id": self.evaluation_request_id,
                **self.identity_dict()}


@dataclass(frozen=True)
class MetricResult:
    metric_id: str
    value: float
    threshold: float
    direction: str = "maximize"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", _identifier(self.metric_id, "metric ID"))
        if self.direction not in {"maximize", "minimize"}:
            raise ValueError("metric direction must be maximize or minimize")
        for name in ("value", "threshold"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError("metric values must be finite")
            object.__setattr__(self, name, value)

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold if self.direction == "maximize" else self.value <= self.threshold

    def to_dict(self) -> dict:
        return {**self.__dict__, "passed": self.passed}


@dataclass(frozen=True)
class EvaluationReport:
    request: EvaluationRequest
    provider_id: str
    status: str
    metrics: tuple[MetricResult, ...] = ()
    evaluated_cases: int = 0
    failed_cases: int = 0
    error_code: str = ""
    evaluation_report_id: str = field(init=False)
    schema: str = EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request, EvaluationRequest):
            raise TypeError("request must be EvaluationRequest")
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider ID"))
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported evaluation status")
        metrics = tuple(self.metrics)
        if not all(isinstance(item, MetricResult) for item in metrics):
            raise TypeError("metrics must contain MetricResult values")
        metrics = tuple(sorted(metrics, key=lambda item: item.metric_id))
        if len({item.metric_id for item in metrics}) != len(metrics):
            raise ValueError("metric IDs must be unique")
        object.__setattr__(self, "metrics", metrics)
        for name in ("evaluated_cases", "failed_cases"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, int(value))
        if self.failed_cases > self.evaluated_cases:
            raise ValueError("failed_cases cannot exceed evaluated_cases")
        if self.error_code:
            object.__setattr__(self, "error_code", _identifier(self.error_code, "error code"))
        if self.status == "completed":
            if not metrics or {item.metric_id for item in metrics} != set(self.request.metric_ids):
                raise ValueError("completed evaluation must report every requested metric")
            if self.error_code:
                raise ValueError("completed evaluation cannot contain an error code")
        elif metrics or self.evaluated_cases or self.failed_cases:
            raise ValueError("non-completed evaluation cannot claim result metrics or cases")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed evaluation requires an error code")
        if self.status != "failed" and self.error_code:
            raise ValueError("only failed evaluation can contain an error code")
        object.__setattr__(self, "evaluation_report_id", _identity(
            "eval_", self.identity_dict()))

    @property
    def passed(self) -> bool:
        return (self.status == "completed" and self.failed_cases == 0 and
                all(item.passed for item in self.metrics))

    def identity_dict(self) -> dict:
        return {"schema": self.schema, "request": self.request.to_dict(),
                "provider_id": self.provider_id, "status": self.status,
                "metrics": [item.to_dict() for item in self.metrics],
                "evaluated_cases": self.evaluated_cases,
                "failed_cases": self.failed_cases, "error_code": self.error_code}

    def to_dict(self) -> dict:
        return {"evaluation_report_id": self.evaluation_report_id,
                "passed": self.passed, **self.identity_dict()}


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    kind: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider ID"))
        if self.kind not in {"evaluation", "training"}:
            raise ValueError("provider kind must be evaluation or training")
        capabilities = tuple(sorted({_identifier(item, "provider capability")
                                     for item in self.capabilities}))
        if not capabilities:
            raise ValueError("provider must declare at least one capability")
        object.__setattr__(self, "capabilities", capabilities)

    def to_dict(self) -> dict:
        return {**self.__dict__, "capabilities": list(self.capabilities)}


@runtime_checkable
class EvalProvider(Protocol):
    def profile(self) -> ProviderProfile: ...
    async def evaluate(self, request: EvaluationRequest) -> EvaluationReport: ...


@runtime_checkable
class TrainingRuntime(Protocol):
    def profile(self) -> ProviderProfile: ...
    async def submit(self, request: TrainingRequest) -> TrainingRun: ...


class LifecycleContractConflict(ValueError):
    pass


def prompt_package_from_dict(value: Mapping[str, Any]) -> PromptPackage:
    if value.get("schema") != PROMPT_SCHEMA:
        raise ValueError("unsupported prompt package schema")
    try:
        package = PromptPackage(
            name=value["name"], version=value["version"],
            messages=tuple(PromptMessage(**item) for item in value["messages"]),
            variables=tuple(value.get("variables") or ()),
            output_contract=value["output_contract"],
            metadata=tuple(dict(value.get("metadata") or {}).items()))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed prompt package") from exc
    if value.get("prompt_package_id") != package.prompt_package_id:
        raise LifecycleContractConflict("prompt package identity does not match content")
    return package


def training_request_from_dict(value: Mapping[str, Any]) -> TrainingRequest:
    try:
        request = TrainingRequest(
            dataset_revision_id=value["dataset_revision_id"], objective=value["objective"],
            base_model_package_id=value.get("base_model_package_id", ""),
            prompt_package_id=value.get("prompt_package_id", ""),
            hyperparameters=tuple(dict(value.get("hyperparameters") or {}).items()))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed training request") from exc
    if value.get("training_request_id") != request.training_request_id:
        raise LifecycleContractConflict("training request identity does not match content")
    return request


def training_run_from_dict(value: Mapping[str, Any]) -> TrainingRun:
    if value.get("schema") != TRAINING_SCHEMA:
        raise ValueError("unsupported training run schema")
    try:
        return TrainingRun(
            training_run_id=value["training_run_id"],
            request=training_request_from_dict(value["request"]),
            runtime_id=value["runtime_id"], status=value["status"],
            attempts=value.get("attempts", 0),
            output_model_package_id=value.get("output_model_package_id", ""),
            evaluation_report_ids=tuple(value.get("evaluation_report_ids") or ()),
            error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed training run") from exc


def evaluation_request_from_dict(value: Mapping[str, Any]) -> EvaluationRequest:
    try:
        request = EvaluationRequest(
            subject_kind=value["subject_kind"], subject_id=value["subject_id"],
            dataset_revision_id=value["dataset_revision_id"],
            metric_ids=tuple(value["metric_ids"]),
            prompt_package_id=value.get("prompt_package_id", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed evaluation request") from exc
    if value.get("evaluation_request_id") != request.evaluation_request_id:
        raise LifecycleContractConflict("evaluation request identity does not match content")
    return request


def evaluation_report_from_dict(value: Mapping[str, Any]) -> EvaluationReport:
    if value.get("schema") != EVALUATION_SCHEMA:
        raise ValueError("unsupported evaluation report schema")
    try:
        metrics = tuple(MetricResult(
            metric_id=item["metric_id"], value=item["value"],
            threshold=item["threshold"], direction=item.get("direction", "maximize"))
                        for item in value.get("metrics") or ())
        for raw, metric in zip(value.get("metrics") or (), metrics):
            if raw.get("passed") is not metric.passed:
                raise LifecycleContractConflict("metric pass flag does not match values")
        report = EvaluationReport(
            request=evaluation_request_from_dict(value["request"]),
            provider_id=value["provider_id"], status=value["status"], metrics=metrics,
            evaluated_cases=value.get("evaluated_cases", 0),
            failed_cases=value.get("failed_cases", 0),
            error_code=value.get("error_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed evaluation report") from exc
    if value.get("evaluation_report_id") != report.evaluation_report_id:
        raise LifecycleContractConflict("evaluation report identity does not match content")
    if value.get("passed") is not report.passed:
        raise LifecycleContractConflict("evaluation pass flag does not match metrics")
    return report
