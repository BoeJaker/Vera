"""Shared package-bound adapter for legacy JSON batch prediction seams."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from .inference_contracts import (
    InferenceCancellation, InferenceContractConflict, InferenceEvent,
    InferenceRequest, InferenceValue)
from .model_package import ModelPackage, _identifier
from .training_contracts import ProviderProfile

LegacyPredictionRunner = Callable[[str, str], Awaitable[Mapping[str, Any]]]


class LegacyBatchPredictionProvider:
    """Normalize a bound legacy ``(selector, JSON)`` prediction function."""

    def __init__(self, package: ModelPackage, *, provider_id: str,
                 binding: str, runner: LegacyPredictionRunner,
                 input_name: str = "X",
                 metadata_fields: tuple[tuple[str, str], ...] = ()):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if not isinstance(binding, str) or not binding.strip() or len(binding) > 1024:
            raise ValueError("legacy prediction binding must be a bounded string")
        if not callable(runner):
            raise TypeError("legacy prediction runner must be callable")
        self._input_name = _identifier(input_name, "legacy prediction input name")
        fields = tuple((_identifier(source, "legacy metadata source"),
                        _identifier(target, "portable metadata target"))
                       for source, target in metadata_fields)
        if len(fields) > 32 or len({target for _, target in fields}) != len(fields):
            raise ValueError("legacy metadata targets must be unique and bounded")
        self._package = package
        self._binding = binding.strip()
        self._runner = runner
        self._metadata_fields = fields
        self._profile = ProviderProfile(
            provider_id, "inference", package.compatibility.tasks)

    def profile(self) -> ProviderProfile:
        return self._profile

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
                output = self._normalize_output(response)
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

    def _validate_request(self, request: InferenceRequest) -> InferenceValue:
        if request.model_package_id != self._package.package_id:
            raise InferenceContractConflict(
                "legacy prediction request package does not match binding")
        compatibility = self._package.compatibility
        if (request.task not in compatibility.tasks or
                request.input_contract != compatibility.input_contract or
                request.output_contract != compatibility.output_contract):
            raise InferenceContractConflict(
                "legacy prediction request is incompatible with package")
        if request.stream:
            raise InferenceContractConflict("legacy batch prediction is not streaming")
        if request.parameters:
            raise InferenceContractConflict(
                "legacy batch prediction has no portable parameters")
        if len(request.inputs) != 1 or request.inputs[0].name != self._input_name \
                or not request.inputs[0].json_data:
            raise InferenceContractConflict(
                f"legacy batch prediction requires one inline JSON {self._input_name} input")
        return request.inputs[0]

    def _normalize_output(self, response: Mapping[str, Any]) -> InferenceValue:
        shape = response["shape"]
        if (not isinstance(shape, (list, tuple)) or len(shape) > 32 or
                any(isinstance(size, bool) or not isinstance(size, int) or size < 0
                    for size in shape)):
            raise ValueError("invalid prediction shape")
        value = {"predictions": response["predictions"], "shape": list(shape)}
        for source, target in self._metadata_fields:
            metadata = response.get(source)
            if metadata is not None and not isinstance(metadata, (str, int, float, bool)):
                raise ValueError("invalid prediction metadata")
            value[target] = metadata
        return InferenceValue.from_json("result", value)

    def _failed(self, request: InferenceRequest, code: str) -> InferenceEvent:
        return InferenceEvent(
            request.request_id, request.model_package_id,
            self._profile.provider_id, 0, "failed", error_code=code)
