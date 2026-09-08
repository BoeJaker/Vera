from dataclasses import replace

import pytest

from vera.models import (
    InferenceContractConflict, InferenceRequest, InferenceValue,
    ModelDeploymentTarget, ModelTrustPolicy, PyTorchInferenceProvider,
    TensorFlowInferenceProvider, consume_inference, define_inference_deployment,
    evaluate_model_admission)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage

pytestmark = pytest.mark.critical


def package(runtime="pytorch", *, fmt=None, framework=None, version="v1"):
    fmt = fmt or runtime
    framework = framework or runtime
    return ModelPackage(
        f"{runtime}-model", version, "dense", fmt,
        (ModelArtifact("weights", f"artifact://{runtime}/weights",
                       "c" * 64, 12),),
        ModelCompatibility(("predict",), "tensor/v1", "predictions/v1"),
        framework=framework, framework_version="2")


def deployment(value, runtime):
    target = ModelDeploymentTarget(
        f"{runtime}:worker", "predict", "tensor/v1", "predictions/v1",
        1_000_000, frameworks=(value.framework,))
    admission = evaluate_model_admission(
        value, ModelTrustPolicy("default"), target)
    assert admission.accepted
    return define_inference_deployment(
        value, admission, provider_id=f"{runtime}:provider",
        runtime_kind=runtime, runtime_version="2.0.0", placements=("local",),
        retry_owner="inference-router")


def request(value):
    return InferenceRequest(
        value.package_id, "predict", "tensor/v1", "predictions/v1",
        (InferenceValue.from_json("X", [[1, 2]]),), max_output_bytes=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime,provider_type", [
    ("pytorch", PyTorchInferenceProvider),
    ("tensorflow", TensorFlowInferenceProvider),
])
async def test_native_adapter_dispatches_only_to_injected_deployment_runner(
        runtime, provider_type):
    value = package(runtime)
    deployed = deployment(value, runtime)
    calls = []

    async def runner(deployment_id, payload):
        calls.append((deployment_id, payload))
        return {"ok": True, "predictions": [[0.75]], "shape": [1, 1],
                "runtime_secret": "must-not-leak"}

    provider = provider_type(value, deployed, runner=runner)
    result = await consume_inference(provider, request(value))
    assert calls == [(deployed.deployment_id, "[[1,2]]")]
    assert result.status == "completed"
    assert result.outputs[0].to_dict()["value"] == {
        "predictions": [[0.75]], "shape": [1, 1]}
    assert "runtime_secret" not in result.outputs[0].json_data
    assert provider.profile().provider_id == deployed.provider_id


@pytest.mark.parametrize("provider_type,runtime,bad_format,bad_framework", [
    (PyTorchInferenceProvider, "pytorch", "tensorflow", "pytorch"),
    (PyTorchInferenceProvider, "pytorch", "pytorch", "tensorflow"),
    (TensorFlowInferenceProvider, "tensorflow", "pytorch", "tensorflow"),
    (TensorFlowInferenceProvider, "tensorflow", "tensorflow", "pytorch"),
])
def test_native_adapters_reject_wrong_package_format_or_framework(
        provider_type, runtime, bad_format, bad_framework):
    value = package(runtime, fmt=bad_format, framework=bad_framework)
    deployed = deployment(value, runtime)
    with pytest.raises(InferenceContractConflict, match="incompatible"):
        provider_type(value, deployed, runner=lambda *_: None)


def test_native_adapter_rejects_foreign_package_runtime_and_artifact_evidence():
    value = package("pytorch")
    deployed = deployment(value, "pytorch")
    other = package("pytorch", version="v2")
    with pytest.raises(InferenceContractConflict, match="another package"):
        PyTorchInferenceProvider(other, deployed, runner=lambda *_: None)
    with pytest.raises(InferenceContractConflict, match="runtime"):
        TensorFlowInferenceProvider(value, deployed, runner=lambda *_: None)
    forged = replace(deployed, artifact_digests=(("weights", "d" * 64),))
    with pytest.raises(InferenceContractConflict, match="artifact evidence"):
        PyTorchInferenceProvider(value, forged, runner=lambda *_: None)


@pytest.mark.asyncio
async def test_native_adapter_retains_shared_request_and_failure_boundaries():
    value = package("pytorch", fmt="safetensors", framework="torch")
    deployed = deployment(value, "pytorch")
    calls = 0

    async def runner(*_):
        nonlocal calls
        calls += 1
        raise RuntimeError("private runtime failure")

    provider = PyTorchInferenceProvider(value, deployed, runner=runner)
    failed = await consume_inference(provider, request(value))
    assert (failed.status, failed.error_code) == ("failed", "backend_error")
    wrong = InferenceRequest(
        value.package_id, "predict", "tensor/v1", "predictions/v1",
        (InferenceValue.from_json("X", [[1]]),), stream=True)
    with pytest.raises(InferenceContractConflict, match="not streaming"):
        await consume_inference(provider, wrong)
    assert calls == 1
