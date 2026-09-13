import asyncio

import pytest

from vera.models import (
    InferenceContractConflict, InferenceProvider, InferenceRequest,
    InferenceValue, LegacyOllamaInferenceProvider, consume_inference)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage

pytestmark = pytest.mark.critical


def package(tasks=("generate",)):
    return ModelPackage(
        "model", "v1", "transformer", "gguf",
        (ModelArtifact("weights", "file:///model.gguf", "a" * 64, 10),),
        ModelCompatibility(tasks, "prompt/v1", "text/v1"))


def request(value, *, prompt="hello", system=None, stream=False, parameters=(),
            model=None, task=None, input_contract=None, output_contract=None):
    inputs = [InferenceValue.from_json("prompt", prompt)]
    if system is not None:
        inputs.append(InferenceValue.from_json("system", system))
    return InferenceRequest(
        model or value.package_id, task or value.compatibility.tasks[0],
        input_contract or value.compatibility.input_contract,
        output_contract or value.compatibility.output_contract,
        tuple(inputs), parameters=parameters, stream=stream,
        max_output_bytes=1_000_000)


class Runner:
    def __init__(self, result="hello", chunks=(), meta=None, error=None):
        self.result = result
        self.chunks = chunks
        self.meta = meta or {}
        self.error = error
        self.calls = []

    async def __call__(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs.copy()))
        if self.error:
            raise self.error
        callback = kwargs["stream_cb"]
        if callback:
            for chunk in self.chunks:
                await callback(chunk)
        kwargs["meta_out"].update(self.meta)
        return self.result


def provider(value, runner):
    return LegacyOllamaInferenceProvider(
        value, provider_id="ollama:gpu", model="qwen:9b",
        instance_id="gpu-250", runner=runner)


@pytest.mark.asyncio
async def test_non_stream_generation_maps_bound_call_and_usage():
    value = package()
    runner = Runner("answer", meta={"eval_count": 3, "total_ms": 99})
    result = await consume_inference(
        provider(value, runner), request(
            value, system="be concise",
            parameters=(("json_mode", True), ("max_tokens", 20),
                        ("temperature", 0), ("repetition_penalty", 1.1),
                        ("keep_alive", "5m"), ("think", False))))
    assert result.status == "completed"
    assert [item.to_dict()["value"] for item in result.outputs] == ["answer"]
    assert dict(result.usage) == {"output_tokens": 3}
    prompt, call = runner.calls[0]
    assert prompt == "hello"
    assert call == {
        "system": "be concise", "json_mode": True, "model": "qwen:9b",
        "instance_id": "gpu-250", "prefer_gpu": False, "stream_cb": None,
        "options": {"num_predict": 20, "repeat_penalty": 1.1,
                    "temperature": 0},
        "keep_alive": "5m", "think": False, "meta_out": call["meta_out"]}
    assert call["meta_out"] == {"eval_count": 3, "total_ms": 99}


@pytest.mark.asyncio
async def test_streaming_preserves_deltas_without_repeating_aggregate_result():
    value = package()
    runner = Runner("hello", chunks=("hel", "lo"), meta={"eval_count": 2})
    result = await consume_inference(provider(value, runner), request(value, stream=True))
    assert [item.to_dict()["value"] for item in result.outputs] == ["hel", "lo"]
    assert result.status == "completed"
    assert runner.calls[0][1]["stream_cb"] is not None


@pytest.mark.asyncio
async def test_streaming_falls_back_to_returned_text_when_legacy_callback_is_silent():
    value = package()
    result = await consume_inference(
        provider(value, Runner("answer")), request(value, stream=True))
    assert [item.to_dict()["value"] for item in result.outputs] == ["answer"]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_truncation_is_a_failed_terminal_after_preserving_partial_output():
    value = package()
    result = await consume_inference(
        provider(value, Runner("partial", meta={"truncated": True, "eval_count": 5})),
        request(value))
    assert [item.to_dict()["value"] for item in result.outputs] == ["partial"]
    assert (result.status, result.error_code, result.usage) == (
        "failed", "output_truncated", ())


@pytest.mark.asyncio
@pytest.mark.parametrize("change,message", [
    ({"model": "mpkg_other"}, "package"),
    ({"task": "chat"}, "requested task"),
    ({"input_contract": "wrong/v1"}, "incompatible"),
    ({"output_contract": "wrong/v1"}, "incompatible"),
])
async def test_adapter_rejects_incompatible_request_before_runner(change, message):
    value = package()
    runner = Runner()
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(provider(value, runner), request(value, **change))
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,system,parameters,message", [
    (3, None, (), "must be text"),
    ("x", 3, (), "must be text"),
    ("x", None, (("unknown", 1),), "unsupported"),
    ("x", None, (("max_tokens", 0),), "outside"),
    ("x", None, (("top_p", 2),), "outside"),
    ("x", None, (("json_mode", "yes"),), "boolean"),
    ("x", None, (("keep_alive", ""),), "bounded"),
])
async def test_inputs_and_parameters_are_validated_before_runner(
        prompt, system, parameters, message):
    value = package()
    runner = Runner()
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(
            provider(value, runner),
            request(value, prompt=prompt, system=system, parameters=parameters))
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runner,error_code", [
    (Runner(error=RuntimeError("sensitive backend detail")), "backend_error"),
    (Runner(result=3), "invalid_response"),
    (Runner(result=""), "empty_response"),
    (Runner(meta={"eval_count": -1}), "invalid_usage"),
])
async def test_legacy_failures_are_reduced_to_stable_codes(runner, error_code):
    value = package()
    result = await consume_inference(provider(value, runner), request(value))
    assert (result.status, result.error_code) == ("failed", error_code)


@pytest.mark.asyncio
async def test_cancelling_consumer_cancels_injected_runner():
    value = package()
    cancelled = asyncio.Event()

    async def runner(_prompt, **_kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def consume():
        await consume_inference(provider(value, runner), request(value, stream=True))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_closing_backpressured_stream_reaps_injected_runner():
    value = package()
    reaped = asyncio.Event()

    async def runner(_prompt, **kwargs):
        try:
            for _ in range(2000):
                await kwargs["stream_cb"]("x")
        finally:
            reaped.set()
        return "x" * 2000

    stream = provider(value, runner).infer(request(value, stream=True))
    assert (await anext(stream)).kind == "output"
    await asyncio.sleep(0)
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert reaped.is_set()


def test_constructor_requires_supported_tasks_explicit_bindings_and_runner():
    with pytest.raises(ValueError, match="unsupported"):
        provider(package(("embed",)), Runner())
    value = package()
    with pytest.raises(ValueError, match="model selector"):
        LegacyOllamaInferenceProvider(
            value, provider_id="ollama:gpu", model="", instance_id="gpu", runner=Runner())
    with pytest.raises(ValueError, match="instance binding"):
        LegacyOllamaInferenceProvider(
            value, provider_id="ollama:gpu", model="model", instance_id="", runner=Runner())
    with pytest.raises(TypeError, match="callable"):
        LegacyOllamaInferenceProvider(
            value, provider_id="ollama:gpu", model="model", instance_id="gpu", runner=None)
    assert isinstance(provider(value, Runner()), InferenceProvider)
