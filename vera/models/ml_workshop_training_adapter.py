"""Deterministic boundary between ML Workshop jobs and portable training runs.

The adapter deliberately knows nothing about numpy, torch, TensorFlow, or the
Workshop implementation.  Callers inject the native submit/read/stop seams so
importing and testing this module cannot start training or load a model.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Mapping

from vera.execution.run_projection import ShadowRunRegistry
from vera.execution.run_protocol import ArtifactRef, Run, RunError, RunStatus

from .model_package import ModelPackage, model_package_from_dict
from .training_contracts import ProviderProfile, TrainingRequest, TrainingRun


NativeCall = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


def _identifier(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 256 or any(ch.isspace() for ch in value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _stable_id(runtime_id: str, request_id: str) -> str:
    encoded = json.dumps(
        {"runtime_id": runtime_id, "training_request_id": request_id},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "trun_" + hashlib.sha256(encoded).hexdigest()


async def _call(function: NativeCall, *args: Any) -> Mapping[str, Any]:
    result = function(*args)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise ValueError("ML Workshop response must be a mapping")
    return result


@dataclass(frozen=True)
class MLWorkshopTrainingBinding:
    """Explicit authority to train one portable base package in one module."""

    base_model_package_id: str
    module_id: str
    objectives: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_model_package_id", _identifier(
            self.base_model_package_id, "base model package ID"))
        object.__setattr__(self, "module_id", _identifier(self.module_id, "module ID"))
        objectives = tuple(sorted({_identifier(value, "objective")
                                   for value in self.objectives}))
        if not objectives:
            raise ValueError("binding must allow at least one objective")
        object.__setattr__(self, "objectives", objectives)


@dataclass(frozen=True)
class MLWorkshopTrainingSubmission:
    """Payload-safe native submission: identities and scalar configuration only."""

    training_run_id: str
    training_request_id: str
    dataset_revision_id: str
    module_id: str
    objective: str
    base_model_package_id: str
    prompt_package_id: str
    hyperparameters: tuple[tuple[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "training_run_id": self.training_run_id,
            "training_request_id": self.training_request_id,
            "dataset_revision_id": self.dataset_revision_id,
            "module_id": self.module_id,
            "objective": self.objective,
            "base_model_package_id": self.base_model_package_id,
            "prompt_package_id": self.prompt_package_id,
            "hyperparameters": dict(self.hyperparameters),
        }


@dataclass
class _State:
    request: TrainingRequest
    native_job_id: str
    run: Run
    output_model_package_id: str = ""
    evaluation_report_ids: tuple[str, ...] = ()


class MLWorkshopTrainingRuntime:
    """Portable, fail-closed projection over an injected ML Workshop runner."""

    _NATIVE_STATUS = {
        "queued": "queued",
        "running": "running",
        "complete": "succeeded",
        "completed": "succeeded",
        "early_stopped": "succeeded",
        "stopped": "cancelled",
        "cancelled": "cancelled",
        "error": "failed",
        "failed": "failed",
    }

    def __init__(self, *, runtime_id: str, bindings: tuple[MLWorkshopTrainingBinding, ...],
                 submitter: NativeCall, status_reader: NativeCall, stopper: NativeCall,
                 registry: ShadowRunRegistry | None = None) -> None:
        self.runtime_id = _identifier(runtime_id, "runtime ID")
        self._submitter = submitter
        self._status_reader = status_reader
        self._stopper = stopper
        self._registry = registry or ShadowRunRegistry()
        self._bindings = {item.base_model_package_id: item for item in bindings}
        if not self._bindings or len(self._bindings) != len(bindings):
            raise ValueError("bindings must be non-empty and unique by package ID")
        self._states: dict[str, _State] = {}

    def profile(self) -> ProviderProfile:
        return ProviderProfile(provider_id=self.runtime_id, kind="training",
                               capabilities=("submit", "observe", "cancel"))

    async def submit(self, request: TrainingRequest) -> TrainingRun:
        if not isinstance(request, TrainingRequest):
            raise TypeError("request must be TrainingRequest")
        binding = self._bindings.get(request.base_model_package_id)
        if binding is None:
            raise ValueError("no ML Workshop binding for base ModelPackage")
        if request.objective not in binding.objectives:
            raise ValueError("training objective is not allowed by the binding")
        training_run_id = _stable_id(self.runtime_id, request.training_request_id)
        existing = self._states.get(training_run_id)
        if existing:
            return self._training_run(existing, existing.run.status)

        submission = MLWorkshopTrainingSubmission(
            training_run_id=training_run_id,
            training_request_id=request.training_request_id,
            dataset_revision_id=request.dataset_revision_id,
            module_id=binding.module_id,
            objective=request.objective,
            base_model_package_id=request.base_model_package_id,
            prompt_package_id=request.prompt_package_id,
            hyperparameters=request.hyperparameters,
        )
        response = await _call(self._submitter, submission)
        self._verify_identity(response, submission, require_job=True)
        native_job_id = _identifier(response.get("native_job_id"), "native job ID")
        native_status = self._portable_status(response.get("status", "queued"))
        if native_status not in {"queued", "running"}:
            raise ValueError("submission response must be queued or running")

        run = Run(id=training_run_id, kind="vera.training.ml_workshop",
                  task_id=request.training_request_id)
        event = run.transition(
            RunStatus.QUEUED if native_status == "queued" else RunStatus.RUNNING,
            event_type="training.submitted",
            payload={"runtime_id": self.runtime_id, "native_job_id": native_job_id,
                     "training_request_id": request.training_request_id,
                     "dataset_revision_id": request.dataset_revision_id},
        )
        self._registry.record(run, event)
        state = _State(request=request, native_job_id=native_job_id, run=run)
        self._states[training_run_id] = state
        return self._training_run(state, run.status)

    async def observe(self, training_run_id: str) -> TrainingRun:
        state = self._state(training_run_id)
        if state.run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return self._training_run(state, state.run.status)
        response = await _call(self._status_reader, state.native_job_id)
        self._verify_state_response(response, state)
        portable = self._portable_status(response.get("status"))
        target = {"queued": RunStatus.QUEUED, "running": RunStatus.RUNNING,
                  "succeeded": RunStatus.COMPLETED, "failed": RunStatus.FAILED,
                  "cancelled": RunStatus.CANCELLED}[portable]

        if target == RunStatus.QUEUED:
            if state.run.status != RunStatus.QUEUED:
                raise ValueError("ML Workshop job status regressed to queued")
            return self._training_run(state, state.run.status)
        if target == RunStatus.RUNNING:
            self._transition_if_needed(state, RunStatus.RUNNING, "training.started")
            return self._training_run(state, state.run.status)

        # RunProtocol does not permit queued -> terminal. Preserve the real
        # observation while materialising the required intermediate state.
        self._transition_if_needed(state, RunStatus.RUNNING, "training.started")
        if target == RunStatus.COMPLETED:
            if response.get("output_model_package") is None:
                return self._fail(state, "missing_output_model_package")
            try:
                package = self._output_package(response, state)
            except (TypeError, ValueError):
                return self._fail(state, "invalid_output_model_package")
            state.output_model_package_id = package.package_id
            state.evaluation_report_ids = package.evaluation_report_ids
            state.run.artifacts.append(ArtifactRef(
                id=package.package_id, kind="model.package",
                uri=f"model-package://{package.package_id}"))
            state.run.artifacts.extend(ArtifactRef(
                id=report_id, kind="evaluation.report",
                uri=f"evaluation-report://{report_id}")
                for report_id in package.evaluation_report_ids)
            self._transition(state, RunStatus.COMPLETED, "training.completed",
                             {"output_model_package_id": package.package_id,
                              "evaluation_report_ids": list(package.evaluation_report_ids)})
        elif target == RunStatus.FAILED:
            return self._fail(state, _identifier(
                response.get("error_code") or "native_training_failed", "error code"))
        else:
            self._transition(state, RunStatus.CANCELLED, "training.cancelled", {})
        return self._training_run(state, state.run.status)

    async def cancel(self, training_run_id: str) -> TrainingRun:
        state = self._state(training_run_id)
        if state.run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return self._training_run(state, state.run.status)
        response = await _call(self._stopper, state.native_job_id)
        self._verify_state_response(response, state)
        if self._portable_status(response.get("status")) != "cancelled":
            raise ValueError("ML Workshop did not acknowledge cancellation")
        self._transition(state, RunStatus.CANCELLED, "training.cancelled", {})
        return self._training_run(state, state.run.status)

    def run_evidence(self, training_run_id: str) -> Mapping[str, Any] | None:
        self._state(training_run_id)
        return self._registry.get(training_run_id)

    def _state(self, training_run_id: str) -> _State:
        state = self._states.get(_identifier(training_run_id, "training run ID"))
        if state is None:
            raise KeyError("unknown training run")
        return state

    def _verify_identity(self, value: Mapping[str, Any], submission: MLWorkshopTrainingSubmission,
                         *, require_job: bool) -> None:
        expected = {
            "training_run_id": submission.training_run_id,
            "training_request_id": submission.training_request_id,
            "dataset_revision_id": submission.dataset_revision_id,
        }
        for key, identity in expected.items():
            if value.get(key) != identity:
                raise ValueError(f"ML Workshop {key} does not match submission")
        if require_job:
            _identifier(value.get("native_job_id"), "native job ID")

    def _verify_state_response(self, value: Mapping[str, Any], state: _State) -> None:
        submission = MLWorkshopTrainingSubmission(
            training_run_id=state.run.id,
            training_request_id=state.request.training_request_id,
            dataset_revision_id=state.request.dataset_revision_id,
            module_id=self._bindings[state.request.base_model_package_id].module_id,
            objective=state.request.objective,
            base_model_package_id=state.request.base_model_package_id,
            prompt_package_id=state.request.prompt_package_id,
            hyperparameters=state.request.hyperparameters,
        )
        self._verify_identity(value, submission, require_job=True)
        if value.get("native_job_id") != state.native_job_id:
            raise ValueError("ML Workshop native job ID does not match submitted job")

    def _portable_status(self, value: Any) -> str:
        status = self._NATIVE_STATUS.get(str(value or "").strip().lower())
        if status is None:
            raise ValueError("unsupported ML Workshop job status")
        return status

    def _output_package(self, value: Mapping[str, Any], state: _State) -> ModelPackage:
        raw = value.get("output_model_package")
        package = raw if isinstance(raw, ModelPackage) else model_package_from_dict(raw)
        if package.training_run_id != state.run.id:
            raise ValueError("output ModelPackage belongs to another training run")
        return package

    def _transition_if_needed(self, state: _State, target: RunStatus, event_type: str) -> None:
        if state.run.status != target:
            self._transition(state, target, event_type, {})

    def _transition(self, state: _State, target: RunStatus, event_type: str,
                    payload: Mapping[str, Any]) -> None:
        event = state.run.transition(target, event_type=event_type, payload=payload)
        self._registry.record(state.run, event)

    def _fail(self, state: _State, error_code: str) -> TrainingRun:
        state.run.error = RunError(code=error_code, message="ML Workshop training failed")
        self._transition(state, RunStatus.FAILED, "training.failed",
                         {"error_code": error_code})
        return self._training_run(state, state.run.status)

    def _training_run(self, state: _State, status: RunStatus) -> TrainingRun:
        mapped = {RunStatus.QUEUED: "queued", RunStatus.RUNNING: "running",
                  RunStatus.COMPLETED: "succeeded", RunStatus.FAILED: "failed",
                  RunStatus.CANCELLED: "cancelled"}.get(status)
        if mapped is None:
            raise ValueError("run has no portable training status")
        error = state.run.error.code if status == RunStatus.FAILED and state.run.error else ""
        return TrainingRun(training_run_id=state.run.id, request=state.request,
                           runtime_id=self.runtime_id, status=mapped,
                           attempts=state.run.attempt,
                           output_model_package_id=state.output_model_package_id,
                           evaluation_report_ids=state.evaluation_report_ids,
                           error_code=error)
