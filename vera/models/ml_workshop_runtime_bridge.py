"""Concrete injected bridge from ML Workshop jobs to portable training runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Awaitable, Callable, Mapping

from .ml_workshop_training_adapter import (
    MLWorkshopTrainingBinding, MLWorkshopTrainingRuntime,
    MLWorkshopTrainingSubmission)
from .model_package import (
    InMemoryModelPackageRegistry, ModelArtifact, ModelCompatibility,
    ModelPackage)
from .onnx_import import inspect_and_register_onnx

BridgeCall = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
DatasetResolver = Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
_TERMINAL = {"complete", "completed", "early_stopped", "stopped",
             "cancelled", "error", "failed"}
_ALLOWED_HYPERPARAMETERS = {
    "epochs", "lr", "learning_rate", "batch_size", "optimiser", "loss",
    "lr_schedule", "early_stopping_patience", "weight_decay", "grad_clip",
}


async def _invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    value = function(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _finite_json(value: Any, *, depth: int = 0) -> int:
    if depth > 16:
        raise ValueError("training dataset nesting is too deep")
    if value is None or isinstance(value, (str, bool)):
        raise ValueError("training dataset must contain numeric scalars")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("training dataset contains a non-finite number")
        return 1
    if isinstance(value, list):
        return sum(_finite_json(item, depth=depth + 1) for item in value)
    raise ValueError("training dataset must contain bounded JSON arrays and scalars")


@dataclass(frozen=True)
class MLWorkshopDatasetBatch:
    """Exact dataset revision resolved to bounded Workshop arrays."""
    dataset_revision_id: str
    X_train: list[Any]
    y_train: list[Any]
    X_val: list[Any]
    y_val: list[Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], expected_revision: str, *,
                     maximum_rows: int = 100_000,
                     maximum_scalars: int = 10_000_000,
                     maximum_json_bytes: int = 64 * 1024 * 1024,
                     ) -> "MLWorkshopDatasetBatch":
        if not isinstance(value, Mapping):
            raise ValueError("dataset resolver must return a mapping")
        if value.get("dataset_revision_id") != expected_revision:
            raise ValueError("resolved dataset revision does not match training request")
        arrays: dict[str, list[Any]] = {}
        total = 0
        for name in ("X_train", "y_train", "X_val", "y_val"):
            raw = value.get(name)
            if not isinstance(raw, list):
                raise ValueError(f"resolved dataset {name} must be an array")
            if len(raw) > maximum_rows:
                raise ValueError("resolved training dataset exceeds row limit")
            arrays[name] = raw
            total += _finite_json(raw)
        if not arrays["X_train"] or len(arrays["X_train"]) != len(arrays["y_train"]):
            raise ValueError("training features and labels must be non-empty and aligned")
        if len(arrays["X_val"]) != len(arrays["y_val"]):
            raise ValueError("validation features and labels must be aligned")
        if total > maximum_scalars:
            raise ValueError("resolved training dataset exceeds scalar limit")
        encoded_size = sum(len(json.dumps(
            item, allow_nan=False, separators=(",", ":")).encode("utf-8"))
            for item in arrays.values())
        if encoded_size > maximum_json_bytes:
            raise ValueError("resolved training dataset exceeds encoded size limit")
        return cls(expected_revision, **arrays)


@dataclass(frozen=True)
class MLWorkshopONNXOutputBinding:
    module_id: str
    name: str
    architecture: str
    task: str
    input_contract: str
    output_contract: str

    def __post_init__(self) -> None:
        for name in ("module_id", "name", "architecture", "task",
                     "input_contract", "output_contract"):
            value = str(getattr(self, name) or "").strip()
            if not value or len(value) > 256 or any(ch.isspace() for ch in value):
                raise ValueError(f"{name} must be a bounded identifier")
            object.__setattr__(self, name, value)


class MLWorkshopONNXPublisher:
    """Publish a mutable Workshop export as an immutable registered package."""
    def __init__(self, *, export_call: BridgeCall,
                 artifact_store: str | Path,
                 allowed_export_roots: tuple[str | Path, ...],
                 outputs: tuple[MLWorkshopONNXOutputBinding, ...],
                 registry: Any | None = None,
                 maximum_artifact_bytes: int = 2 * 1024 * 1024 * 1024):
        self._export_call = export_call
        self._store = Path(artifact_store).resolve()
        self._export_roots = tuple(Path(value).resolve(strict=True)
                                   for value in allowed_export_roots)
        if not self._export_roots or any(not value.is_dir()
                                         for value in self._export_roots):
            raise ValueError("allowed export roots must be existing directories")
        self._outputs = {item.module_id: item for item in outputs}
        if not self._outputs or len(self._outputs) != len(outputs):
            raise ValueError("output bindings must be non-empty and unique")
        self._registry = (registry if registry is not None
                          else InMemoryModelPackageRegistry())
        self._maximum = int(maximum_artifact_bytes)
        if self._maximum <= 0:
            raise ValueError("maximum artifact bytes must be positive")

    @property
    def registry(self) -> Any:
        return self._registry

    async def publish(self, submission: MLWorkshopTrainingSubmission,
                      native_job_id: str) -> ModelPackage:
        binding = self._outputs.get(submission.module_id)
        if binding is None:
            raise ValueError("no ONNX output binding for Workshop module")
        result = await _invoke(
            self._export_call, module_id=submission.module_id, register_cap=False)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise ValueError("ML Workshop ONNX export failed")
        if result.get("module_id") != submission.module_id:
            raise ValueError("ML Workshop ONNX export module identity drifted")
        source = Path(str(result.get("path") or "")).expanduser()
        if source.is_symlink():
            raise ValueError("ML Workshop ONNX export must not be a symlink")
        source = source.resolve(strict=True)
        if not any(source.is_relative_to(root) for root in self._export_roots):
            raise ValueError("ML Workshop export is outside its allowed roots")
        if source.suffix.lower() != ".onnx" or not source.is_file():
            raise ValueError("ML Workshop export is not an ONNX file")
        size = source.stat().st_size
        if size <= 0 or size > self._maximum:
            raise ValueError("ML Workshop ONNX export has an invalid size")
        digest = await asyncio.to_thread(self._hash_file, source)
        immutable = await asyncio.to_thread(
            self._publish_immutable, source, digest, size)
        opset = result.get("opset")
        package = ModelPackage(
            name=binding.name,
            version=f"run-{submission.training_run_id.removeprefix('trun_')[:16]}",
            architecture=binding.architecture, format="onnx",
            artifacts=(ModelArtifact(
                role="model", uri=immutable.as_uri(), sha256=digest,
                size_bytes=size),),
            compatibility=ModelCompatibility(
                (binding.task,), binding.input_contract, binding.output_contract),
            framework="onnxruntime", opset=int(opset) if opset is not None else None,
            training_run_id=submission.training_run_id,
            metadata=(("dataset_revision_id", submission.dataset_revision_id),
                      ("module_id", submission.module_id),
                      ("native_job_id", native_job_id)),
        )
        receipt = await asyncio.to_thread(
            inspect_and_register_onnx, package, self._registry)
        if not receipt.registered:
            raise ValueError("published ONNX package failed artifact verification")
        return package

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _publish_immutable(self, source: Path, digest: str, size: int) -> Path:
        self._store.mkdir(parents=True, exist_ok=True)
        destination = self._store / f"{digest}.onnx"
        if destination.exists():
            if (destination.is_symlink() or not destination.is_file() or
                    destination.stat().st_size != size or
                    self._hash_file(destination) != digest):
                raise ValueError("immutable ONNX artifact store contains a conflict")
            return destination.resolve()
        fd, temporary = tempfile.mkstemp(prefix=".onnx-", dir=self._store)
        temp_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
                shutil.copyfileobj(origin, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if temp_path.stat().st_size != size or self._hash_file(temp_path) != digest:
                raise ValueError("ONNX artifact changed while it was published")
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                pass
            if (not destination.exists() or destination.is_symlink() or
                    not destination.is_file() or
                    destination.stat().st_size != size or
                    self._hash_file(destination) != digest):
                raise ValueError("immutable ONNX artifact publication conflicted")
            return destination.resolve()
        finally:
            temp_path.unlink(missing_ok=True)


class MLWorkshopCapabilityBridge:
    """Adapt real Workshop capability shapes to the injected runtime boundary."""
    def __init__(self, *, dataset_resolver: DatasetResolver,
                 train_call: BridgeCall, status_call: BridgeCall,
                 stop_call: BridgeCall, publisher: MLWorkshopONNXPublisher,
                 cancellation_timeout_seconds: float = 10.0,
                 poll_interval_seconds: float = 0.05):
        self._dataset_resolver = dataset_resolver
        self._train_call = train_call
        self._status_call = status_call
        self._stop_call = stop_call
        self._publisher = publisher
        self._timeout = float(cancellation_timeout_seconds)
        self._poll = float(poll_interval_seconds)
        if self._timeout <= 0 or self._poll < 0:
            raise ValueError("cancellation timing must be bounded")
        self._jobs: dict[str, MLWorkshopTrainingSubmission] = {}

    async def submit(self, submission: MLWorkshopTrainingSubmission) -> Mapping[str, Any]:
        raw_dataset = await _invoke(
            self._dataset_resolver, submission.dataset_revision_id)
        dataset = MLWorkshopDatasetBatch.from_mapping(
            raw_dataset, submission.dataset_revision_id)
        config = dict(submission.hyperparameters)
        if set(config) - _ALLOWED_HYPERPARAMETERS:
            raise ValueError("unsupported ML Workshop hyperparameter")
        if "learning_rate" in config:
            if "lr" in config:
                raise ValueError("learning_rate and lr cannot both be supplied")
            config["lr"] = config.pop("learning_rate")
        response = await _invoke(
            self._train_call, module_id=submission.module_id,
            X_train=json.dumps(dataset.X_train, separators=(",", ":")),
            y_train=json.dumps(dataset.y_train, separators=(",", ":")),
            X_val=json.dumps(dataset.X_val, separators=(",", ":")),
            y_val=json.dumps(dataset.y_val, separators=(",", ":")),
            config=json.dumps(config, sort_keys=True, separators=(",", ":")))
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise ValueError("ML Workshop training submission failed")
        if response.get("module_id") != submission.module_id:
            raise ValueError("ML Workshop training module identity drifted")
        job_id = str(response.get("job_id") or "").strip()
        if not job_id or len(job_id) > 256 or any(ch.isspace() for ch in job_id):
            raise ValueError("ML Workshop returned an invalid job ID")
        self._jobs[job_id] = submission
        return self._response(submission, job_id, "queued")

    async def status(self, native_job_id: str) -> Mapping[str, Any]:
        submission = self._submission(native_job_id)
        response = await _invoke(self._status_call, job_id=native_job_id)
        status = self._status(response, submission, native_job_id)
        result = self._response(submission, native_job_id, status)
        if status in {"complete", "completed", "early_stopped"}:
            package = await self._publisher.publish(submission, native_job_id)
            result["output_model_package"] = package.to_dict()
        elif status in {"error", "failed"}:
            result["error_code"] = "native_training_failed"
        return result

    async def stop(self, native_job_id: str) -> Mapping[str, Any]:
        submission = self._submission(native_job_id)
        response = await _invoke(self._stop_call, job_id=native_job_id)
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise ValueError("ML Workshop cancellation request failed")
        if response.get("job_id") != native_job_id:
            raise ValueError("ML Workshop cancellation job identity drifted")
        deadline = time.monotonic() + self._timeout
        while True:
            observed = await _invoke(self._status_call, job_id=native_job_id)
            status = self._status(observed, submission, native_job_id)
            if status in {"stopped", "cancelled"}:
                return self._response(submission, native_job_id, "stopped")
            if status in {"complete", "completed", "early_stopped", "error", "failed"}:
                raise ValueError("ML Workshop reached another terminal state during cancellation")
            if time.monotonic() >= deadline:
                raise TimeoutError("ML Workshop cancellation acknowledgement timed out")
            await asyncio.sleep(self._poll)

    def runtime(self, *, runtime_id: str,
                bindings: tuple[MLWorkshopTrainingBinding, ...],
                registry: Any | None = None) -> MLWorkshopTrainingRuntime:
        return MLWorkshopTrainingRuntime(
            runtime_id=runtime_id, bindings=bindings,
            submitter=self.submit, status_reader=self.status, stopper=self.stop,
            registry=registry)

    def _submission(self, native_job_id: str) -> MLWorkshopTrainingSubmission:
        try:
            return self._jobs[native_job_id]
        except KeyError as exc:
            raise KeyError("unknown ML Workshop bridge job") from exc

    @staticmethod
    def _status(response: Any, submission: MLWorkshopTrainingSubmission,
                native_job_id: str) -> str:
        if not isinstance(response, Mapping):
            raise ValueError("ML Workshop status response must be a mapping")
        if response.get("job_id") != native_job_id:
            raise ValueError("ML Workshop status job identity drifted")
        if response.get("module_id") != submission.module_id:
            raise ValueError("ML Workshop status module identity drifted")
        status = str(response.get("status") or "").strip().lower()
        if status not in _TERMINAL | {"queued", "running", "stopping"}:
            raise ValueError("ML Workshop returned an unsupported status")
        return status

    @staticmethod
    def _response(submission: MLWorkshopTrainingSubmission, native_job_id: str,
                  status: str) -> dict[str, Any]:
        return {"training_run_id": submission.training_run_id,
                "training_request_id": submission.training_request_id,
                "dataset_revision_id": submission.dataset_revision_id,
                "native_job_id": native_job_id, "status": status}
