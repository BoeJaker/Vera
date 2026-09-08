"""Offline compatibility adapter for Vera's legacy Ollama generation seam."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import math
from typing import Any

from .inference_contracts import (
    InferenceCancellation, InferenceContractConflict, InferenceEvent,
    InferenceRequest, InferenceValue)
from .model_package import ModelPackage
from .training_contracts import ProviderProfile

LegacyOllamaRunner = Callable[..., Awaitable[str]]
_TASKS = {"generate", "completion"}
_PARAMETERS = {
    "json_mode", "keep_alive", "max_tokens", "repetition_penalty", "think",
    "temperature", "top_k", "top_p",
}


class LegacyOllamaInferenceProvider:
    """Bind one package, model and instance to an injected ``ollama_generate``."""

    def __init__(self, package: ModelPackage, *, provider_id: str, model: str,
                 instance_id: str, runner: LegacyOllamaRunner):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        unsupported = set(package.compatibility.tasks) - _TASKS
        if unsupported:
            raise ValueError("package declares tasks unsupported by legacy Ollama")
        for value, name in ((model, "model selector"),
                            (instance_id, "instance binding")):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"{name} must be a bounded string")
        if not callable(runner):
            raise TypeError("legacy Ollama runner must be callable")
        self._package = package
        self._model = model.strip()
        self._instance_id = instance_id.strip()
        self._runner = runner
        self._profile = ProviderProfile(
            provider_id, "inference", package.compatibility.tasks)

    def profile(self) -> ProviderProfile:
        return self._profile

    def infer(self, request: InferenceRequest, *,
              cancellation: InferenceCancellation | None = None
              ) -> AsyncIterator[InferenceEvent]:
        async def events() -> AsyncIterator[InferenceEvent]:
            prompt, system, parameters = self._validate_request(request)
            queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=1024)
            finished = object()
            meta: dict[str, Any] = {}
            outcome: dict[str, Any] = {}

            async def on_chunk(chunk: Any) -> None:
                if not isinstance(chunk, str):
                    raise TypeError("legacy Ollama stream chunk must be text")
                if chunk:
                    await queue.put(chunk)

            async def run() -> None:
                try:
                    outcome["text"] = await self._runner(
                        prompt, system=system,
                        json_mode=parameters.pop("json_mode", False),
                        model=self._model, instance_id=self._instance_id,
                        prefer_gpu=False,
                        stream_cb=on_chunk if request.stream else None,
                        options=self._options(parameters),
                        keep_alive=parameters.pop("keep_alive", None),
                        think=parameters.pop("think", None), meta_out=meta)
                except (asyncio.CancelledError, Exception) as exc:
                    outcome["error"] = exc
                finally:
                    await queue.put(finished)

            if cancellation is not None:
                cancellation.checkpoint()
            task = asyncio.create_task(run())
            sequence = 0
            chunks = 0
            try:
                if request.stream:
                    while True:
                        item = await queue.get()
                        if item is finished:
                            break
                        if cancellation is not None:
                            cancellation.checkpoint()
                        try:
                            output = self._output(item)
                        except (TypeError, ValueError, RecursionError):
                            task.cancel()
                            try:
                                await task
                            except BaseException:
                                pass
                            yield self._failed(request, sequence, "invalid_output")
                            return
                        yield InferenceEvent(
                            request.request_id, request.model_package_id,
                            self._profile.provider_id, sequence, "output", output=output)
                        sequence += 1
                        chunks += 1
                await task
            except (asyncio.CancelledError, GeneratorExit):
                task.cancel()
                # The producer may be blocked behind stream backpressure. Drain
                # its private queue so its ``finally`` can publish completion
                # and the owned task can always be reaped.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    await task
                except BaseException:
                    pass
                raise
            error = outcome.get("error")
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                raise error
            if error is not None:
                yield self._failed(request, sequence, "backend_error")
                return
            text = outcome.get("text")
            if not isinstance(text, str):
                yield self._failed(request, sequence, "invalid_response")
                return
            if not request.stream or not chunks:
                try:
                    output = self._output(text)
                except (TypeError, ValueError, RecursionError):
                    yield self._failed(request, sequence, "invalid_output")
                    return
                if text:
                    yield InferenceEvent(
                        request.request_id, request.model_package_id,
                        self._profile.provider_id, sequence, "output", output=output)
                    sequence += 1
            if meta.get("truncated") is True:
                yield self._failed(request, sequence, "output_truncated")
                return
            try:
                usage = self._usage(meta)
            except (TypeError, ValueError):
                yield self._failed(request, sequence, "invalid_usage")
                return
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                self._profile.provider_id, sequence, "completed", usage=usage)
        return events()

    def _validate_request(self, request: InferenceRequest
                          ) -> tuple[str, str, dict[str, Any]]:
        if request.model_package_id != self._package.package_id:
            raise InferenceContractConflict(
                "Ollama request package does not match binding")
        compatibility = self._package.compatibility
        if (request.task not in compatibility.tasks or
                request.input_contract != compatibility.input_contract or
                request.output_contract != compatibility.output_contract):
            raise InferenceContractConflict("Ollama request is incompatible with package")
        if request.task not in _TASKS:
            raise InferenceContractConflict("unsupported Ollama inference task")
        inputs = {value.name: value for value in request.inputs}
        if set(inputs) not in ({"prompt"}, {"prompt", "system"}) \
                or any(not value.json_data for value in inputs.values()):
            raise InferenceContractConflict(
                "Ollama generation requires inline prompt and optional system inputs")
        try:
            prompt = json.loads(inputs["prompt"].json_data)
            system = json.loads(inputs["system"].json_data) if "system" in inputs else ""
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceContractConflict("Ollama inputs must be text") from exc
        if not isinstance(prompt, str) or not isinstance(system, str):
            raise InferenceContractConflict("Ollama inputs must be text")
        parameters = dict(request.parameters)
        if set(parameters) - _PARAMETERS:
            raise InferenceContractConflict("unsupported portable Ollama parameter")
        self._validate_parameters(parameters)
        return prompt, system, parameters

    @staticmethod
    def _validate_parameters(parameters: dict[str, Any]) -> None:
        for key in ("json_mode", "think"):
            if key in parameters and not isinstance(parameters[key], bool):
                raise InferenceContractConflict(f"{key} must be boolean")
        for key, minimum, maximum in (
                ("max_tokens", 1, 1_000_000), ("top_k", -1, 1_000_000)):
            if key in parameters and (isinstance(parameters[key], bool)
                                      or not isinstance(parameters[key], int)
                                      or not minimum <= parameters[key] <= maximum):
                raise InferenceContractConflict(f"{key} is outside its portable bounds")
        for key, minimum, maximum, lower_open in (
                ("temperature", 0, 100, False), ("top_p", 0, 1, False),
                ("repetition_penalty", 0, 100, True)):
            if key not in parameters:
                continue
            value = parameters[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value > maximum \
                    or (value <= minimum if lower_open else value < minimum):
                raise InferenceContractConflict(f"{key} is outside its portable bounds")
        if "keep_alive" in parameters and (
                not isinstance(parameters["keep_alive"], str)
                or not parameters["keep_alive"]
                or len(parameters["keep_alive"]) > 128):
            raise InferenceContractConflict("keep_alive must be a bounded string")

    @staticmethod
    def _options(parameters: dict[str, Any]) -> dict[str, Any]:
        rename = {"max_tokens": "num_predict",
                  "repetition_penalty": "repeat_penalty"}
        return {rename.get(key, key): value for key, value in parameters.items()
                if key not in {"json_mode", "keep_alive", "think"}}

    @staticmethod
    def _output(text: Any) -> InferenceValue:
        if not isinstance(text, str):
            raise TypeError("Ollama output must be text")
        return InferenceValue.from_json("text", text, media_type="text/plain")

    @staticmethod
    def _usage(meta: dict[str, Any]) -> tuple[tuple[str, int], ...]:
        if "eval_count" not in meta:
            return ()
        count = meta["eval_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid Ollama usage")
        return (("output_tokens", count),)

    def _failed(self, request: InferenceRequest, sequence: int,
                code: str) -> InferenceEvent:
        return InferenceEvent(
            request.request_id, request.model_package_id,
            self._profile.provider_id, sequence, "failed", error_code=code)
