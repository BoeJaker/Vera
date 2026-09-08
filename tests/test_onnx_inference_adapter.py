import asyncio

import pytest

from vera.models import (
    InferenceContractConflict, InferenceProvider, InferenceRequest,
    InferenceValue, LegacyONNXInferenceProvider, consume_inference,
)
from vera.models.model_package import (
    ModelArtifact, ModelCompatibility, ModelPackage,
)

pytestmark = pytest.mark.critical


def package(*, fmt="onnx"):
    return ModelPackage(
        "model", "v1", "linear", fmt,
        (ModelArtifact("model", "file:///model.onnx", "a" * 64, 10),),
        ModelCompatibility(("predict",), "tensor/v1", "predictions/v1"))


def request(value, *, model=None, task="predict", input_contract="tensor/v1",
            output_contract="predictions/v1", stream=False, parameters=()):
    return InferenceRequest(
        model or value.package_id, task, input_contract, output_contract,
        (InferenceValue.from_json("X", [[1, 2]]),), parameters=parameters,
        stream=stream, max_output_bytes=1000)


@pytest.mark.asyncio
async def test_adapter_maps_injected_legacy_result_without_importing_runtime():
    value = package()
    calls = []
    async def runner(binding, payload):
        calls.append((binding, payload))
        return {"ok": True, "predictions": [[3]], "shape": [1, 1],
                "provider": "CPUExecutionProvider", "ignored": "legacy detail"}
    provider = LegacyONNXInferenceProvider(
        value, binding="linear-model", runner=runner)
    result = await consume_inference(provider, request(value))
    assert calls == [("linear-model", "[[1,2]]")]
    assert result.status == "completed"
    assert result.outputs[0].to_dict()["value"] == {
        "predictions": [[3]], "shape": [1, 1],
        "execution_provider": "CPUExecutionProvider"}
    assert "ignored" not in result.outputs[0].json_data
    assert isinstance(provider, InferenceProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize("change, message", [
    ({"model": "mpkg_other"}, "package"),
    ({"task": "generate"}, "requested task"),
    ({"input_contract": "text/v1"}, "incompatible"),
    ({"output_contract": "text/v1"}, "incompatible"),
    ({"stream": True}, "not streaming"),
    ({"parameters": (("temperature", 0),)}, "no portable parameters"),
])
async def test_adapter_rejects_incompatible_requests_before_runner(change, message):
    value = package()
    calls = 0
    async def runner(*_):
        nonlocal calls
        calls += 1
        return {"ok": True, "predictions": [], "shape": [0]}
    provider = LegacyONNXInferenceProvider(value, binding="model", runner=runner)
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(provider, request(value, **change))
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response, error_code", [
    ({"error": "sensitive path"}, "backend_rejected"),
    ("not an object", "invalid_response"),
    ({"ok": True, "predictions": [1], "shape": "wrong"}, "invalid_output"),
    ({"ok": True, "predictions": [float("nan")], "shape": [1]},
     "invalid_output"),
])
async def test_adapter_bounds_legacy_failures_to_stable_codes(response, error_code):
    value = package()
    async def runner(*_):
        return response
    result = await consume_inference(
        LegacyONNXInferenceProvider(value, binding="model", runner=runner),
        request(value))
    assert (result.status, result.error_code, result.outputs) == (
        "failed", error_code, ())


@pytest.mark.asyncio
async def test_backend_exception_is_bounded_and_cancellation_propagates():
    value = package()
    async def broken(*_):
        raise RuntimeError("sensitive backend detail")
    failed = await consume_inference(
        LegacyONNXInferenceProvider(value, binding="model", runner=broken),
        request(value))
    assert (failed.status, failed.error_code) == ("failed", "backend_error")

    async def cancelled(*_):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await consume_inference(
            LegacyONNXInferenceProvider(value, binding="model", runner=cancelled),
            request(value))


def test_adapter_requires_onnx_package_and_callable_runner():
    with pytest.raises(ValueError, match="onnx ModelPackage"):
        LegacyONNXInferenceProvider(package(fmt="tensorflow"), binding="model",
                                    runner=lambda *_: None)
    with pytest.raises(TypeError, match="callable"):
        LegacyONNXInferenceProvider(package(), binding="model", runner=None)
