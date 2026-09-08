import asyncio

import pytest

from vera.models import (
    InferenceContractConflict, InferenceProvider, InferenceRequest,
    InferenceValue, OpenAICompatibleInferenceProvider,
    OpenAICompatibleTransport, consume_inference,
)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage

pytestmark = pytest.mark.critical


def package(task="generate"):
    contracts = {
        "generate": ("text/v1", "text/v1"),
        "completion": ("text/v1", "text/v1"),
        "chat": ("messages/v1", "message/v1"),
        "embed": ("text/v1", "embedding/v1"),
        "embedding": ("text/v1", "embedding/v1"),
    }
    input_contract, output_contract = contracts[task]
    return ModelPackage(
        "model", "v1", "transformer", "safetensors",
        (ModelArtifact("weights", "file:///model.safetensors", "a" * 64, 10),),
        ModelCompatibility((task,), input_contract, output_contract))


def request(value, input_name, input_value, *, stream=False, parameters=(),
            model=None, task=None, input_contract=None, output_contract=None):
    return InferenceRequest(
        model or value.package_id, task or value.compatibility.tasks[0],
        input_contract or value.compatibility.input_contract,
        output_contract or value.compatibility.output_contract,
        (InferenceValue.from_json(input_name, input_value),),
        parameters=parameters, stream=stream, max_output_bytes=1_000_000)


class Transport:
    def __init__(self, responses=(), error=None):
        self.responses = responses
        self.error = error
        self.calls = []

    async def request(self, path, payload, *, stream, cancellation=None):
        self.calls.append((path, dict(payload), stream, cancellation))
        if self.error:
            raise self.error
        for response in self.responses:
            yield response


def provider(value, transport):
    return OpenAICompatibleInferenceProvider(
        value, provider_id="vllm:gpu", model="org/model", transport=transport)


@pytest.mark.asyncio
async def test_completion_stream_maps_chunks_and_usage_without_transport_policy():
    value = package()
    transport = Transport([
        {"choices": [{"text": "hel"}]},
        {"choices": [{"text": "lo", "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ])
    result = await consume_inference(
        provider(value, transport),
        request(value, "prompt", "Say hello", stream=True,
                parameters=(("temperature", 0), ("max_tokens", 8))))
    assert [output.to_dict()["value"] for output in result.outputs] == ["hel", "lo"]
    assert result.status == "completed"
    assert dict(result.usage) == {"completion_tokens": 2, "prompt_tokens": 3}
    assert transport.calls == [(
        "/v1/completions",
        {"model": "org/model", "prompt": "Say hello", "max_tokens": 8,
         "temperature": 0, "stream": True}, True, None)]


@pytest.mark.asyncio
async def test_chat_stream_accepts_terminal_choice_without_content():
    value = package("chat")
    transport = Transport([
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])
    result = await consume_inference(
        provider(value, transport),
        request(value, "messages", [{"role": "user", "content": "Hello"}],
                stream=True))
    assert [item.to_dict()["value"] for item in result.outputs] == ["Hi"]
    assert result.status == "completed"
    assert transport.calls[0][0] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_non_stream_chat_and_embeddings_have_portable_outputs():
    chat_package = package("chat")
    chat = await consume_inference(
        provider(chat_package, Transport([
            {"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}])),
        request(chat_package, "messages", [{"role": "user", "content": "Hello"}]))
    assert chat.outputs[0].to_dict()["value"] == "Hi"

    embed_package = package("embed")
    embed_transport = Transport([{
        "data": [{"index": 1, "embedding": [0.3]},
                 {"index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 2, "total_tokens": 2}}])
    embedded = await consume_inference(
        provider(embed_package, embed_transport),
        request(embed_package, "input", ["one", "two"],
                parameters=(("dimensions", 2),)))
    assert embedded.outputs[0].to_dict()["value"] == [[0.1, 0.2], [0.3]]
    assert embed_transport.calls[0][0] == "/v1/embeddings"
    assert embed_transport.calls[0][1]["stream"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change,message", [
    ({"model": "mpkg_other"}, "package"),
    ({"task": "chat"}, "requested task"),
    ({"input_contract": "wrong/v1"}, "incompatible"),
    ({"output_contract": "wrong/v1"}, "incompatible"),
])
async def test_binding_rejects_incompatible_request_before_transport(change, message):
    value = package()
    transport = Transport([{"choices": [{"text": "wrong"}]}])
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(
            provider(value, transport), request(value, "prompt", "x", **change))
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task,input_name,input_value,stream,parameters,message", [
    ("generate", "wrong", "x", False, (), "prompt input"),
    ("generate", "prompt", 3, False, (), "prompt must be"),
    ("chat", "messages", [], False, (), "bounded non-empty"),
    ("chat", "messages", [{"role": "user", "content": "x", "extra": 1}],
     False, (), "unsupported role or field"),
    ("embed", "input", [1], False, (), "text or a text list"),
    ("embed", "input", "x", True, (), "not streaming"),
    ("generate", "prompt", "x", False, (("arbitrary", 1),), "unsupported"),
    ("generate", "prompt", "x", False, (("top_p", 2),), "outside"),
    ("embed", "input", "x", False, (("encoding_format", "hex"),),
     "encoding format"),
])
async def test_request_shape_and_parameter_validation_is_fail_closed(
        task, input_name, input_value, stream, parameters, message):
    value = package(task)
    transport = Transport([])
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(
            provider(value, transport),
            request(value, input_name, input_value, stream=stream,
                    parameters=parameters))
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("responses,error_code", [
    ([{"error": {"message": "secret backend detail"}}], "backend_rejected"),
    (["wrong"], "invalid_response"),
    ([{"choices": "wrong"}], "invalid_output"),
    ([{"choices": [{"text": 7}]}], "invalid_output"),
    ([{"data": [{"index": 0, "embedding": [float("nan")]}]}],
     "invalid_output"),
    ([], "invalid_response"),
])
async def test_backend_failures_are_reduced_to_stable_codes(responses, error_code):
    task = "embed" if responses and isinstance(responses[0], dict) \
        and "data" in responses[0] else "generate"
    value = package(task)
    input_name, input_value = ("input", "x") if task == "embed" else ("prompt", "x")
    result = await consume_inference(
        provider(value, Transport(responses)), request(value, input_name, input_value))
    assert (result.status, result.error_code, result.outputs) == (
        "failed", error_code, ())


@pytest.mark.asyncio
async def test_transport_exception_is_bounded_and_cancellation_propagates():
    value = package()
    failed = await consume_inference(
        provider(value, Transport(error=RuntimeError("secret"))),
        request(value, "prompt", "x"))
    assert (failed.status, failed.error_code) == ("failed", "transport_error")

    cancelled = Transport(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await consume_inference(
            provider(value, cancelled), request(value, "prompt", "x"))


@pytest.mark.asyncio
async def test_transport_must_be_async_and_raw_events_are_bounded():
    value = package()

    class WrongTransport:
        def request(self, *_args, **_kwargs):
            return []

    result = await consume_inference(
        provider(value, WrongTransport()), request(value, "prompt", "x"))
    assert (result.status, result.error_code) == ("failed", "invalid_response")

    class EndlessEmptyTransport:
        async def request(self, *_args, **_kwargs):
            for _ in range(10_001):
                yield {"choices": []}

    result = await consume_inference(
        provider(value, EndlessEmptyTransport()), request(value, "prompt", "x"))
    assert (result.status, result.error_code) == (
        "failed", "response_limit_exceeded")


def test_constructor_requires_supported_package_and_transport_protocol():
    value = package()
    with pytest.raises(TypeError, match="transport"):
        OpenAICompatibleInferenceProvider(
            value, provider_id="vllm:gpu", model="model", transport=None)
    with pytest.raises(ValueError, match="selector"):
        OpenAICompatibleInferenceProvider(
            value, provider_id="vllm:gpu", model="", transport=Transport())
    unsupported = ModelPackage(
        "model", "v1", "transformer", "gguf",
        (ModelArtifact("weights", "file:///model.gguf", "b" * 64, 10),),
        ModelCompatibility(("classify",), "text/v1", "class/v1"))
    with pytest.raises(ValueError, match="unsupported"):
        provider(unsupported, Transport())
    assert isinstance(provider(value, Transport()), InferenceProvider)
    assert isinstance(Transport(), OpenAICompatibleTransport)
