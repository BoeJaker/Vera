"""Offline compatibility adapter for Vera's legacy ONNX inference result shape."""
from __future__ import annotations

from .legacy_prediction_adapter import (
    LegacyBatchPredictionProvider, LegacyPredictionRunner)
from .model_package import ModelPackage

LegacyONNXRunner = LegacyPredictionRunner


class LegacyONNXInferenceProvider(LegacyBatchPredictionProvider):
    """Map one ONNX package/binding to an injected ``ml.onnx.run`` runner."""

    def __init__(self, package: ModelPackage, *, binding: str,
                 runner: LegacyONNXRunner):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if package.format != "onnx":
            raise ValueError("legacy ONNX provider requires an onnx ModelPackage")
        super().__init__(
            package, provider_id=f"onnx:{binding}", binding=binding,
            runner=runner, input_name="X",
            metadata_fields=(("provider", "execution_provider"),))
