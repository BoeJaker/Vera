"""Offline compatibility adapter for Vera's legacy ONNX inference result shape."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from .inference_contracts import (
    InferenceCancellation,
    InferenceContractConflict,
    InferenceEvent,
    InferenceRequest,
    InferenceValue,
)
from .model_package import ModelPackage
from .training_contracts import ProviderProfile

LegacyONNXRunner = Callable[[str, str], Awaitable[Mapping[str, Any]]]


class LegacyONNXInferenceProvider:
    """Map one package/binding to an injected legacy ``ml.onnx.run`` runner."""

    def __init__(self, package: ModelPackage, *, binding: str,
                 runner: LegacyONNXRunner):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if package.format != "onnx":
            raise ValueError("legacy ONNX provider requires an onnx ModelPackage")
        if not callable(runner):
            raise TypeError("legacy ONNX runner must be callable")
        self._profile = ProviderProfile(
            f"onnx:{binding}", "inference", package.compatibility.tasks)
        self._package = package
        self._binding = binding
        self._runner = runner

    def profile(self) -> ProviderProfile:
        return self._profile

    def _validate_request(self, request: InferenceRequest) -> InferenceValue:
        if request.model_package_id != self._package.package_id:
            raise InferenceContractConflict("ONNX request package does not match binding")
        compatibility = self._package.compatibility
        if (request.task not in compatibility.tasks or
                request.input_contract != compatibility.input_contract or
                request.output_contract != compatibility.output_contract):
            raise InferenceContractConflict("ONNX request is incompatible with package")
        if request.stream:
            raise InferenceContractConflict("legacy ONNX inference is not streaming")
        if request.parameters:
            raise InferenceContractConflict("legacy ONNX inference has no portable parameters")
        if len(request.inputs) != 1 or request.inputs[0].name != "X" \
                or not request.inputs[0].json_data:
            raise InferenceContractConflict(
                "legacy ONNX inference requires one inline JSON X input")
        return request.inputs[0]

    def infer(self, request: InferenceRequest, *,
              cancellation: InferenceCancellation | None = None
              ) -> AsyncIterator[InferenceEvent]:
        async def events() -> AsyncIterator[InferenceEvent]:
            value = self._validate_request(request)
            if cancellation is not None:
                cancellation.checkpoint()
            try:
                response = await self._runner(self._binding, value.json_data)
                if cancellation is not None:
                    cancellation.checkpoint()
            except asyncio.CancelledError:
                raise
            except Exception:
                yield self._failed(request, "backend_error")
                return
            if not isinstance(response, Mapping):
                yield self._failed(request, "invalid_response")
                return
            if response.get("ok") is not True:
                yield self._failed(request, "backend_rejected")
                return
            try:
                shape = response["shape"]
                if (not isinstance(shape, (list, tuple)) or len(shape) > 32 or
                        any(isinstance(size, bool) or not isinstance(size, int)
                            or size < 0 for size in shape)):
                    raise ValueError("invalid ONNX shape")
                execution_provider = response.get("provider")
                if execution_provider is not None and not isinstance(
                        execution_provider, str):
                    raise ValueError("invalid ONNX execution provider")
                output = InferenceValue.from_json(
                    "result", {"predictions": response["predictions"],
                               "shape": list(shape),
                               "execution_provider": execution_provider})
            except (KeyError, TypeError, ValueError, RecursionError):
                yield self._failed(request, "invalid_output")
                return
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                self._profile.provider_id, 0, "output", output=output)
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                self._profile.provider_id, 1, "completed")
        return events()

    def _failed(self, request: InferenceRequest, code: str) -> InferenceEvent:
        return InferenceEvent(
            request.request_id, request.model_package_id,
            self._profile.provider_id, 0, "failed", error_code=code)
