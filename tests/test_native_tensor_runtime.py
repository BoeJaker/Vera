import hashlib
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from vera.models import (
    InferenceContractConflict, InferenceRequest, InferenceValue,
    ModelDeploymentTarget, ModelTrustPolicy, NativeTensorRuntimeError,
    consume_inference, define_inference_deployment, evaluate_model_admission,
    load_pytorch_provider, load_tensorflow_provider, verify_native_artifact)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage


pytestmark = pytest.mark.critical


def package(path, runtime, serialization):
    raw = path.read_bytes()
    return ModelPackage(
        f"{runtime}-fixture", "v1", "dense", runtime,
        (ModelArtifact("model", f"artifact://fixtures/{path.name}",
                       hashlib.sha256(raw).hexdigest(), len(raw)),),
        ModelCompatibility(("predict",), "tensor/v1", "predictions/v1"),
        framework=runtime, framework_version="test",
        metadata=(("serialization", serialization),))


def deployment(value, runtime):
    target = ModelDeploymentTarget(
        f"{runtime}:cpu", "predict", "tensor/v1", "predictions/v1",
        1_000_000, frameworks=(runtime,))
    admission = evaluate_model_admission(
        value, ModelTrustPolicy("fixtures"), target)
    assert admission.accepted
    return define_inference_deployment(
        value, admission, provider_id=f"{runtime}:verified",
        runtime_kind=runtime, runtime_version="test", placements=("cpu",),
        retry_owner="inference-router")


def request(value):
    return InferenceRequest(
        value.package_id, "predict", "tensor/v1", "predictions/v1",
        (InferenceValue.from_json("X", [[1.0, 2.0]]),))


class FakeTensor:
    shape = (1, 1)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self

    def tolist(self):
        return [[3.0]]


class FakeModel:
    def eval(self):
        return None

    def __call__(self, _value, **_kwargs):
        return FakeTensor()


@pytest.mark.asyncio
async def test_verified_torchscript_loader_dispatches_bound_cpu_runner(tmp_path):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"bounded-torchscript-fixture")
    value = package(artifact, "pytorch", "torchscript")
    deployed = deployment(value, "pytorch")
    calls = []
    runtime = SimpleNamespace(
        __version__="test",
        jit=SimpleNamespace(load=lambda path, map_location: (
            calls.append((path, map_location)) or FakeModel())),
        inference_mode=lambda: nullcontext(), as_tensor=lambda batch: batch)

    provider = load_pytorch_provider(
        value, deployed, artifact_path=artifact, runtime=runtime)
    result = await consume_inference(provider, request(value))

    assert calls == [(str(artifact.resolve()), "cpu")]
    assert result.status == "completed"
    assert result.outputs[0].to_dict()["value"] == {
        "predictions": [[3.0]], "shape": [1, 1]}


@pytest.mark.asyncio
async def test_verified_safe_keras_loader_dispatches_bound_cpu_runner(tmp_path):
    artifact = tmp_path / "model.keras"
    artifact.write_bytes(b"bounded-keras-v3-fixture")
    value = package(artifact, "tensorflow", "keras-v3")
    deployed = deployment(value, "tensorflow")
    calls = []
    runtime = SimpleNamespace(
        __version__="test",
        keras=SimpleNamespace(models=SimpleNamespace(
            load_model=lambda path, compile, safe_mode: (
                calls.append((path, compile, safe_mode)) or FakeModel()))),
        convert_to_tensor=lambda batch: batch)

    provider = load_tensorflow_provider(
        value, deployed, artifact_path=artifact, runtime=runtime)
    result = await consume_inference(provider, request(value))

    assert calls == [(str(artifact.resolve()), False, True)]
    assert result.status == "completed"
    assert result.outputs[0].to_dict()["value"] == {
        "predictions": [[3.0]], "shape": [1, 1]}


def test_native_loader_rejects_digest_size_symlink_and_unsafe_formats(tmp_path):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"trusted")
    value = package(artifact, "pytorch", "torchscript")
    artifact.write_bytes(b"tampered")
    with pytest.raises(NativeTensorRuntimeError, match="size|digest"):
        verify_native_artifact(value, artifact)

    target = tmp_path / "target.pt"
    target.write_bytes(b"target")
    link = tmp_path / "link.pt"
    try:
        link.symlink_to(target)
    except OSError:
        pass
    else:
        linked = package(target, "pytorch", "torchscript")
        with pytest.raises(NativeTensorRuntimeError, match="symlink"):
            verify_native_artifact(linked, link)

    keras = tmp_path / "model.keras"
    keras.write_bytes(b"keras")
    tf_package = package(keras, "tensorflow", "saved-model")
    with pytest.raises(InferenceContractConflict, match="keras-v3"):
        load_tensorflow_provider(
            tf_package, deployment(tf_package, "tensorflow"),
            artifact_path=keras, runtime=SimpleNamespace(__version__="test"))

    pickle = tmp_path / "model.pt"
    pickle.write_bytes(b"pickle-is-never-loaded")
    torch_package = package(pickle, "pytorch", "pickle")
    with pytest.raises(InferenceContractConflict, match="torchscript"):
        load_pytorch_provider(
            torch_package, deployment(torch_package, "pytorch"),
            artifact_path=pickle, runtime=SimpleNamespace(__version__="test"))


def test_native_loader_rejects_runtime_version_drift(tmp_path):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"valid-fixture")
    value = package(artifact, "pytorch", "torchscript")
    with pytest.raises(NativeTensorRuntimeError, match="version"):
        load_pytorch_provider(
            value, deployment(value, "pytorch"), artifact_path=artifact,
            runtime=SimpleNamespace(__version__="different"))


def test_native_loader_refuses_missing_runtime_after_artifact_verification(
        tmp_path, monkeypatch):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"valid-fixture")
    value = package(artifact, "pytorch", "torchscript")
    def missing(_name):
        raise ImportError("not installed")
    monkeypatch.setattr(
        "vera.models.native_tensor_runtime.importlib.import_module", missing)
    with pytest.raises(NativeTensorRuntimeError, match="runtime is not installed"):
        load_pytorch_provider(
            value, deployment(value, "pytorch"), artifact_path=artifact)
