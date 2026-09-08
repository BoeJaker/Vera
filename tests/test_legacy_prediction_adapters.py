import asyncio

import pytest

from vera.models import (
    InferenceContractConflict, InferenceProvider, InferenceRequest,
    InferenceValue, LegacyBatchPredictionProvider,
    LegacyMLWorkshopInferenceProvider, consume_inference)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage

pytestmark = pytest.mark.critical


def package(fmt="pytorch"):
    suffix = "pt" if fmt == "pytorch" else fmt
    return ModelPackage(
        "workshop-model", "v1", "dense", fmt,
        (ModelArtifact("weights", f"file:///weights.{suffix}", "b" * 64, 10),),
        ModelCompatibility(("predict",), "tensor/v1", "predictions/v1"))


def request(value, *, stream=False, parameters=(), name="X"):
    return InferenceRequest(
        value.package_id, "predict", "tensor/v1", "predictions/v1",
        (InferenceValue.from_json(name, [[1, 2]]),),
        stream=stream, parameters=parameters, max_output_bytes=1000)


@pytest.mark.asyncio
async def test_ml_workshop_adapter_uses_shared_normalization_and_exact_module_binding():
    value = package()
    calls = []

    async def runner(module_id, payload):
        calls.append((module_id, payload))
        return {"ok": True, "module_id": module_id, "n": 1,
                "predictions": [[0.25]], "shape": [1, 1],
                "ignored": "legacy implementation detail"}

    provider = LegacyMLWorkshopInferenceProvider(
        value, module_id="dense-1", runner=runner)
    result = await consume_inference(provider, request(value))
    assert calls == [("dense-1", "[[1,2]]")]
    assert result.status == "completed"
    assert result.outputs[0].to_dict()["value"] == {
        "predictions": [[0.25]], "shape": [1, 1]}
    assert "module_id" not in result.outputs[0].json_data
    assert "ignored" not in result.outputs[0].json_data
    assert provider.profile().provider_id == "ml-workshop:dense-1"
    assert isinstance(provider, InferenceProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,error_code", [
    ({"error": "module missing"}, "backend_rejected"),
    ("wrong", "invalid_response"),
    ({"ok": True, "predictions": [1], "shape": "wrong"}, "invalid_output"),
    ({"ok": True, "predictions": [float("nan")], "shape": [1]},
     "invalid_output"),
])
async def test_shared_adapter_reduces_legacy_failures_to_stable_codes(
        response, error_code):
    value = package()

    async def runner(*_):
        return response

    result = await consume_inference(
        LegacyMLWorkshopInferenceProvider(value, module_id="dense", runner=runner),
        request(value))
    assert (result.status, result.error_code, result.outputs) == (
        "failed", error_code, ())


@pytest.mark.asyncio
async def test_shared_adapter_propagates_cancellation_and_bounds_exceptions():
    value = package()

    async def cancelled(*_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await consume_inference(
            LegacyMLWorkshopInferenceProvider(
                value, module_id="dense", runner=cancelled), request(value))

    async def broken(*_):
        raise RuntimeError("sensitive native detail")

    failed = await consume_inference(
        LegacyMLWorkshopInferenceProvider(value, module_id="dense", runner=broken),
        request(value))
    assert (failed.status, failed.error_code) == ("failed", "backend_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("change,message", [
    ({"stream": True}, "not streaming"),
    ({"parameters": (("batch_size", 4),)}, "no portable parameters"),
    ({"name": "input"}, "inline JSON X"),
])
async def test_shared_adapter_rejects_unsupported_request_shapes_before_runner(
        change, message):
    value = package()
    calls = 0

    async def runner(*_):
        nonlocal calls
        calls += 1
        return {"ok": True, "predictions": [], "shape": [0]}

    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(
            LegacyMLWorkshopInferenceProvider(
                value, module_id="dense", runner=runner), request(value, **change))
    assert calls == 0


def test_shared_adapter_validates_bindings_runner_and_metadata_projection():
    value = package()
    with pytest.raises(ValueError, match="binding"):
        LegacyMLWorkshopInferenceProvider(value, module_id="", runner=lambda *_: None)
    with pytest.raises(TypeError, match="callable"):
        LegacyMLWorkshopInferenceProvider(value, module_id="dense", runner=None)
    with pytest.raises(ValueError, match="unique"):
        LegacyBatchPredictionProvider(
            value, provider_id="native", binding="dense", runner=lambda *_: None,
            metadata_fields=(("one", "same"), ("two", "same")))
