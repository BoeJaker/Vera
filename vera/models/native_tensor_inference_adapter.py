"""Injected PyTorch and TensorFlow batch inference compatibility adapters."""
from __future__ import annotations

from .inference_contracts import InferenceContractConflict
from .inference_deployment import InferenceDeployment
from .legacy_prediction_adapter import (
    LegacyBatchPredictionProvider, LegacyPredictionRunner)
from .model_package import ModelPackage

NativeTensorRunner = LegacyPredictionRunner


class _NativeTensorInferenceProvider(LegacyBatchPredictionProvider):
    """Bind one native runner to an admitted, immutable deployment record."""

    def __init__(self, package: ModelPackage, deployment: InferenceDeployment, *,
                 runner: NativeTensorRunner, runtime_kind: str,
                 formats: frozenset[str], frameworks: frozenset[str]):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if not isinstance(deployment, InferenceDeployment):
            raise TypeError("deployment must be InferenceDeployment")
        if deployment.package_id != package.package_id:
            raise InferenceContractConflict(
                "native inference deployment belongs to another package")
        if deployment.runtime_kind != runtime_kind:
            raise InferenceContractConflict(
                "native inference deployment runtime does not match adapter")
        if package.format not in formats:
            raise InferenceContractConflict(
                "model package format is incompatible with native adapter")
        if package.framework.strip().lower() not in frameworks:
            raise InferenceContractConflict(
                "model package framework is incompatible with native adapter")
        expected_artifacts = tuple((artifact.role, artifact.sha256)
                                   for artifact in package.artifacts)
        if deployment.artifact_digests != expected_artifacts:
            raise InferenceContractConflict(
                "native inference deployment artifact evidence does not match package")
        self.deployment = deployment
        super().__init__(
            package, provider_id=deployment.provider_id,
            binding=deployment.deployment_id, runner=runner, input_name="X")


class PyTorchInferenceProvider(_NativeTensorInferenceProvider):
    """Portable batch seam for an externally owned PyTorch runner."""

    def __init__(self, package: ModelPackage, deployment: InferenceDeployment, *,
                 runner: NativeTensorRunner):
        super().__init__(
            package, deployment, runner=runner, runtime_kind="pytorch",
            formats=frozenset({"pytorch", "safetensors"}),
            frameworks=frozenset({"pytorch", "torch"}))


class TensorFlowInferenceProvider(_NativeTensorInferenceProvider):
    """Portable batch seam for an externally owned TensorFlow runner."""

    def __init__(self, package: ModelPackage, deployment: InferenceDeployment, *,
                 runner: NativeTensorRunner):
        super().__init__(
            package, deployment, runner=runner, runtime_kind="tensorflow",
            formats=frozenset({"tensorflow"}),
            frameworks=frozenset({"tensorflow"}))
