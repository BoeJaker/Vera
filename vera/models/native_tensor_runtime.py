"""Verified, opt-in loaders for deployment-bound native tensor inference.

The portable adapters deliberately do not import framework runtimes.  This
module is the lifecycle boundary an operator may inject when a deployment has
already been admitted.  It accepts one caller-resolved local artifact, verifies
it against the immutable :class:`ModelPackage`, and only then loads a
non-pickle format supported by the selected runtime.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from .inference_contracts import InferenceContractConflict
from .inference_deployment import InferenceDeployment
from .model_package import ModelArtifact, ModelPackage
from .native_tensor_inference_adapter import (
    PyTorchInferenceProvider, TensorFlowInferenceProvider)


class NativeTensorRuntimeError(RuntimeError):
    """A stable refusal at the verified native-runtime boundary."""


def _metadata(package: ModelPackage) -> dict[str, str]:
    return dict(package.metadata)


def _artifact(package: ModelPackage, role: str) -> ModelArtifact:
    matches = [item for item in package.artifacts if item.role == role]
    if len(matches) != 1:
        raise InferenceContractConflict(
            "native runtime requires one declared model artifact role")
    return matches[0]


def verify_native_artifact(package: ModelPackage, artifact_path: str | Path, *,
                           role: str = "model",
                           maximum_bytes: int = 2 * 1024 * 1024 * 1024
                           ) -> Path:
    """Resolve and hash one regular file against immutable package evidence."""
    artifact = _artifact(package, role)
    supplied = Path(artifact_path).expanduser()
    if supplied.is_symlink():
        raise NativeTensorRuntimeError("native model artifact must not be a symlink")
    path = supplied.resolve(strict=True)
    if not path.is_file():
        raise NativeTensorRuntimeError("native model artifact must be a regular file")
    size = path.stat().st_size
    if size != artifact.size_bytes:
        raise NativeTensorRuntimeError("native model artifact size does not match package")
    if size > maximum_bytes:
        raise NativeTensorRuntimeError("native model artifact exceeds loader size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != artifact.sha256:
        raise NativeTensorRuntimeError("native model artifact digest does not match package")
    return path


def _runtime(name: str, supplied: ModuleType | Any | None) -> Any:
    if supplied is not None:
        return supplied
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise NativeTensorRuntimeError(f"{name} runtime is not installed") from exc


def _verify_runtime_version(runtime: Any, package: ModelPackage,
                            deployment: InferenceDeployment) -> None:
    actual = str(getattr(runtime, "__version__", "") or "").strip()
    if (not actual or actual != package.framework_version or
            actual != deployment.runtime_version):
        raise NativeTensorRuntimeError(
            "installed native runtime version does not match deployment evidence")


def _json_batch(payload: str) -> Any:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise NativeTensorRuntimeError("native tensor input is not valid JSON") from exc
    if not isinstance(value, list) or not value:
        raise NativeTensorRuntimeError("native tensor input must be a non-empty batch")
    return value


def load_pytorch_provider(package: ModelPackage, deployment: InferenceDeployment, *,
                          artifact_path: str | Path, artifact_role: str = "model",
                          runtime: Any | None = None) -> PyTorchInferenceProvider:
    """Load a verified CPU TorchScript file and bind it to its deployment."""
    if _metadata(package).get("serialization") != "torchscript":
        raise InferenceContractConflict(
            "PyTorch package must declare serialization=torchscript")
    path = verify_native_artifact(package, artifact_path, role=artifact_role)
    torch = _runtime("torch", runtime)
    _verify_runtime_version(torch, package, deployment)
    try:
        model = torch.jit.load(str(path), map_location="cpu")
        model.eval()
    except Exception as exc:
        raise NativeTensorRuntimeError("TorchScript model load failed") from exc

    async def runner(deployment_id: str, payload: str) -> dict[str, Any]:
        if deployment_id != deployment.deployment_id:
            raise InferenceContractConflict("native runner deployment mismatch")
        batch = _json_batch(payload)

        def predict() -> Any:
            with torch.inference_mode():
                output = model(torch.as_tensor(batch))
            if isinstance(output, (tuple, list)):
                output = output[0]
            return output.detach().cpu()

        output = await asyncio.to_thread(predict)
        return {"ok": True, "predictions": output.tolist(),
                "shape": list(output.shape), "runtime": "pytorch"}

    return PyTorchInferenceProvider(package, deployment, runner=runner)


def load_tensorflow_provider(package: ModelPackage, deployment: InferenceDeployment, *,
                             artifact_path: str | Path, artifact_role: str = "model",
                             runtime: Any | None = None
                             ) -> TensorFlowInferenceProvider:
    """Load a verified safe-mode Keras file and bind it to its deployment."""
    if _metadata(package).get("serialization") != "keras-v3":
        raise InferenceContractConflict(
            "TensorFlow package must declare serialization=keras-v3")
    path = verify_native_artifact(package, artifact_path, role=artifact_role)
    if path.suffix.lower() != ".keras":
        raise NativeTensorRuntimeError("TensorFlow package must use a .keras artifact")
    tensorflow = _runtime("tensorflow", runtime)
    _verify_runtime_version(tensorflow, package, deployment)
    try:
        model = tensorflow.keras.models.load_model(
            str(path), compile=False, safe_mode=True)
    except Exception as exc:
        raise NativeTensorRuntimeError("safe-mode Keras model load failed") from exc

    async def runner(deployment_id: str, payload: str) -> dict[str, Any]:
        if deployment_id != deployment.deployment_id:
            raise InferenceContractConflict("native runner deployment mismatch")
        batch = _json_batch(payload)

        def predict() -> Any:
            return model(tensorflow.convert_to_tensor(batch), training=False)

        output = await asyncio.to_thread(predict)
        array = output.numpy()
        return {"ok": True, "predictions": array.tolist(),
                "shape": list(array.shape), "runtime": "tensorflow"}

    return TensorFlowInferenceProvider(package, deployment, runner=runner)
