"""Portable adapter for injected OpenAI-compatible inference transports."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import json
import math
from typing import Any, Protocol, runtime_checkable

from .inference_contracts import (
    MAX_INFERENCE_EVENTS,
    InferenceCancellation,
    InferenceContractConflict,
    InferenceEvent,
    InferenceRequest,
    InferenceValue,
)
from .model_package import ModelPackage
from .training_contracts import ProviderProfile


@runtime_checkable
class OpenAICompatibleTransport(Protocol):
    """Injected I/O boundary; implementations own HTTP, auth and SSE decoding."""

    def request(self, path: str, payload: Mapping[str, Any], *, stream: bool,
                cancellation: InferenceCancellation | None = None
                ) -> AsyncIterator[Mapping[str, Any]]: ...


_TASKS = {
    "generate": ("/v1/completions", "prompt", "text"),
    "completion": ("/v1/completions", "prompt", "text"),
    "chat": ("/v1/chat/completions", "messages", "message"),
    "embed": ("/v1/embeddings", "input", "embeddings"),
    "embedding": ("/v1/embeddings", "input", "embeddings"),
}
_COMMON_PARAMETERS = {
    "encoding_format", "frequency_penalty", "max_tokens", "presence_penalty",
    "repetition_penalty", "seed", "stop", "temperature", "top_k", "top_p",
}
_EMBED_PARAMETERS = {"dimensions", "encoding_format"}


class OpenAICompatibleInferenceProvider:
    """Bind one ModelPackage and backend model selector to a portable provider."""

    def __init__(self, package: ModelPackage, *, provider_id: str,
                 model: str, transport: OpenAICompatibleTransport):
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if not isinstance(model, str) or not model.strip() or len(model) > 1024:
            raise ValueError("backend model selector must be a bounded string")
        if not isinstance(transport, OpenAICompatibleTransport):
            raise TypeError("transport must implement OpenAICompatibleTransport")
        unsupported = set(package.compatibility.tasks) - set(_TASKS)
        if unsupported:
            raise ValueError("package declares tasks unsupported by this adapter")
        self._package = package
        self._model = model.strip()
        self._transport = transport
        self._profile = ProviderProfile(
            provider_id, "inference", package.compatibility.tasks)

    def profile(self) -> ProviderProfile:
        return self._profile

    def infer(self, request: InferenceRequest, *,
              cancellation: InferenceCancellation | None = None
              ) -> AsyncIterator[InferenceEvent]:
        async def events() -> AsyncIterator[InferenceEvent]:
            path, input_name, output_name = self._validate_request(request)
            payload = self._payload(request, input_name)
            sequence = 0
            usage: dict[str, int] = {}
            saw_response = False
            raw_count = 0
            if cancellation is not None:
                cancellation.checkpoint()
            try:
                source = self._transport.request(
                    path, payload, stream=request.stream,
                    cancellation=cancellation)
                if not hasattr(source, "__aiter__"):
                    raise TypeError("transport did not return an async iterator")
                async for response in source:
                    raw_count += 1
                    if raw_count > MAX_INFERENCE_EVENTS:
                        yield self._failed(request, sequence, "response_limit_exceeded")
                        return
                    if cancellation is not None:
                        cancellation.checkpoint()
                    if not isinstance(response, Mapping):
                        yield self._failed(request, sequence, "invalid_response")
                        return
                    if response.get("error") is not None:
                        yield self._failed(request, sequence, "backend_rejected")
                        return
                    parsed_usage = self._usage(response.get("usage"))
                    if parsed_usage is not None:
                        usage.update(parsed_usage)
                    try:
                        values = self._outputs(request, response, output_name)
                    except (KeyError, TypeError, ValueError, RecursionError):
                        yield self._failed(request, sequence, "invalid_output")
                        return
                    saw_response = saw_response or self._has_result(response)
                    for value in values:
                        yield InferenceEvent(
                            request.request_id, request.model_package_id,
                            self._profile.provider_id, sequence, "output", output=value)
                        sequence += 1
            except asyncio.CancelledError:
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, RecursionError):
                yield self._failed(request, sequence, "invalid_response")
                return
            except Exception:
                yield self._failed(request, sequence, "transport_error")
                return
            if not saw_response:
                yield self._failed(request, sequence, "invalid_response")
                return
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                self._profile.provider_id, sequence, "completed",
                usage=tuple(usage.items()))
        return events()

    def _validate_request(self, request: InferenceRequest) -> tuple[str, str, str]:
        if request.model_package_id != self._package.package_id:
            raise InferenceContractConflict(
                "OpenAI-compatible request package does not match binding")
        compatibility = self._package.compatibility
        if (request.task not in compatibility.tasks or
                request.input_contract != compatibility.input_contract or
                request.output_contract != compatibility.output_contract):
            raise InferenceContractConflict(
                "OpenAI-compatible request is incompatible with package")
        if request.task not in _TASKS:
            raise InferenceContractConflict("unsupported OpenAI-compatible task")
        if request.task in {"embed", "embedding"} and request.stream:
            raise InferenceContractConflict("embedding inference is not streaming")
        path, input_name, output_name = _TASKS[request.task]
        if len(request.inputs) != 1 or request.inputs[0].name != input_name \
                or not request.inputs[0].json_data:
            raise InferenceContractConflict(
                f"{request.task} requires one inline JSON {input_name} input")
        self._validate_parameters(request)
        return path, input_name, output_name

    def _validate_parameters(self, request: InferenceRequest) -> None:
        params = dict(request.parameters)
        allowed = (_EMBED_PARAMETERS if request.task in {"embed", "embedding"}
                   else _COMMON_PARAMETERS)
        unknown = set(params) - allowed
        if unknown:
            raise InferenceContractConflict(
                "unsupported OpenAI-compatible inference parameter")
        self._bounded_int(params, "max_tokens", 1, 1_000_000)
        self._bounded_int(params, "top_k", -1, 1_000_000)
        self._bounded_int(params, "seed", -1, 2**63 - 1)
        self._bounded_int(params, "dimensions", 1, 1_000_000)
        self._bounded_number(params, "temperature", 0, 100)
        self._bounded_number(params, "top_p", 0, 1)
        self._bounded_number(params, "repetition_penalty", 0, 100, lower_open=True)
        self._bounded_number(params, "frequency_penalty", -2, 2)
        self._bounded_number(params, "presence_penalty", -2, 2)
        if "stop" in params and (not isinstance(params["stop"], str)
                                  or len(params["stop"]) > 4096):
            raise InferenceContractConflict("stop must be a bounded string")
        if "encoding_format" in params and params["encoding_format"] not in {
                "float", "base64"}:
            raise InferenceContractConflict("unsupported embedding encoding format")

    @staticmethod
    def _bounded_int(params: Mapping[str, Any], key: str,
                     minimum: int, maximum: int) -> None:
        if key not in params:
            return
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int) \
                or not minimum <= value <= maximum:
            raise InferenceContractConflict(f"{key} is outside its portable bounds")

    @staticmethod
    def _bounded_number(params: Mapping[str, Any], key: str, minimum: float,
                        maximum: float, *, lower_open: bool = False) -> None:
        if key not in params:
            return
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value > maximum \
                or (value <= minimum if lower_open else value < minimum):
            raise InferenceContractConflict(f"{key} is outside its portable bounds")

    def _payload(self, request: InferenceRequest, input_name: str) -> dict[str, Any]:
        value = json.loads(request.inputs[0].json_data)
        if input_name == "prompt" and not isinstance(value, str):
            raise InferenceContractConflict("prompt must be a string")
        if input_name == "messages":
            self._validate_messages(value)
        if input_name == "input" and not (isinstance(value, str) or (
                isinstance(value, list) and len(value) <= 2048 and
                all(isinstance(item, str) for item in value))):
            raise InferenceContractConflict("embedding input must be text or a text list")
        return {"model": self._model, input_name: value,
                **dict(request.parameters), "stream": request.stream}

    @staticmethod
    def _validate_messages(value: Any) -> None:
        if not isinstance(value, list) or not value or len(value) > 1024:
            raise InferenceContractConflict("messages must be a bounded non-empty list")
        allowed_roles = {"developer", "system", "user", "assistant", "tool"}
        allowed_keys = {"role", "content", "name", "tool_call_id", "tool_calls"}
        for message in value:
            if not isinstance(message, dict) or not set(message) <= allowed_keys \
                    or "role" not in message or message.get("role") not in allowed_roles:
                raise InferenceContractConflict(
                    "messages contain an unsupported role or field")
            content = message.get("content")
            if not (isinstance(content, str) or content is None or (
                    isinstance(content, list) and len(content) <= 1024 and
                    all(isinstance(part, dict) for part in content))):
                raise InferenceContractConflict("message content has an invalid shape")
            if content is None and not message.get("tool_calls"):
                raise InferenceContractConflict(
                    "a message without content requires tool calls")
            for key in ("name", "tool_call_id"):
                if key in message and (not isinstance(message[key], str)
                                       or not message[key] or len(message[key]) > 1024):
                    raise InferenceContractConflict(f"message {key} must be bounded text")
            if "tool_calls" in message and (not isinstance(message["tool_calls"], list)
                                              or len(message["tool_calls"]) > 1024):
                raise InferenceContractConflict("message tool calls must be a bounded list")

    @staticmethod
    def _has_result(response: Mapping[str, Any]) -> bool:
        return "choices" in response or "data" in response

    def _outputs(self, request: InferenceRequest, response: Mapping[str, Any],
                 output_name: str) -> list[InferenceValue]:
        if request.task in {"embed", "embedding"}:
            data = response.get("data")
            if not isinstance(data, list) or len(data) > 2048:
                raise ValueError("invalid embedding response")
            indexed: list[tuple[int, Any]] = []
            for position, item in enumerate(data):
                if not isinstance(item, Mapping):
                    raise ValueError("invalid embedding item")
                index = item.get("index", position)
                embedding = item.get("embedding")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0 \
                        or not isinstance(embedding, list) or len(embedding) > 1_000_000 \
                        or any(isinstance(number, bool) or not isinstance(number, (int, float))
                               or not math.isfinite(number) for number in embedding):
                    raise ValueError("invalid embedding item")
                indexed.append((index, embedding))
            if len({index for index, _ in indexed}) != len(indexed):
                raise ValueError("duplicate embedding index")
            return [InferenceValue.from_json(
                output_name, [embedding for _, embedding in sorted(indexed)])]

        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) > 1024:
            raise ValueError("invalid choices")
        outputs: list[InferenceValue] = []
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise ValueError("invalid choice")
            if request.task in {"generate", "completion"}:
                text = choice.get("text")
            elif request.stream:
                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    if choice.get("finish_reason") is not None:
                        continue
                    raise ValueError("invalid chat delta")
                text = delta.get("content")
            else:
                message = choice.get("message")
                if not isinstance(message, Mapping):
                    raise ValueError("invalid chat message")
                text = message.get("content")
            if text is None and choice.get("finish_reason") is not None:
                continue
            if not isinstance(text, str):
                raise ValueError("invalid generated text")
            if text:
                outputs.append(InferenceValue.from_json(output_name, text))
        return outputs

    @staticmethod
    def _usage(value: Any) -> dict[str, int] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or len(value) > 32:
            raise ValueError("invalid usage")
        usage: dict[str, int] = {}
        for key, count in value.items():
            if not isinstance(key, str) or not key or len(key) > 256 \
                    or isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("invalid usage")
            usage[key] = count
        return usage

    def _failed(self, request: InferenceRequest, sequence: int,
                code: str) -> InferenceEvent:
        return InferenceEvent(
            request.request_id, request.model_package_id,
            self._profile.provider_id, sequence, "failed", error_code=code)
