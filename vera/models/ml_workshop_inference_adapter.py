"""Offline compatibility adapter for ML Workshop batch prediction."""
from __future__ import annotations

from .legacy_prediction_adapter import (
    LegacyBatchPredictionProvider, LegacyPredictionRunner)
from .model_package import ModelPackage

LegacyMLWorkshopRunner = LegacyPredictionRunner


class LegacyMLWorkshopInferenceProvider(LegacyBatchPredictionProvider):
    """Map one ModelPackage/module to an injected ``ml.train.predict`` runner."""

    def __init__(self, package: ModelPackage, *, module_id: str,
                 runner: LegacyMLWorkshopRunner):
        super().__init__(
            package, provider_id=f"ml-workshop:{module_id}", binding=module_id,
            runner=runner, input_name="X")
