from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vera.models.ml_workshop_runtime_bridge import (
    MLWorkshopCapabilityBridge,
    MLWorkshopDatasetBatch,
    MLWorkshopONNXOutputBinding,
    MLWorkshopONNXPublisher,
)
from vera.models.ml_workshop_training_adapter import (
    MLWorkshopTrainingBinding,
    MLWorkshopTrainingSubmission,
)
from vera.models.model_package import InMemoryModelPackageRegistry
from vera.models.training_contracts import TrainingRequest


pytestmark = pytest.mark.critical


def dataset(revision: str = "dsrev_exact_7"):
    return {
        "dataset_revision_id": revision,
        "X_train": [[0.0, 1.0], [1.0, 0.0]],
        "y_train": [[1.0], [1.0]],
        "X_val": [[0.0, 0.0]],
        "y_val": [[0.0]],
    }


def request(**overrides):
    values = {
        "dataset_revision_id": "dsrev_exact_7",
        "objective": "classification",
        "base_model_package_id": "mpkg_base",
        "hyperparameters": (("epochs", 3), ("learning_rate", 0.01)),
    }
    values.update(overrides)
    return TrainingRequest(**values)


def output_binding():
    return MLWorkshopONNXOutputBinding(
        module_id="module_classifier",
        name="trained_classifier",
        architecture="tiny_mlp",
        task="classification",
        input_contract="features.v1",
        output_contract="classes.v1",
    )


def runtime(bridge):
    return bridge.runtime(
        runtime_id="ml_workshop",
        bindings=(MLWorkshopTrainingBinding(
            base_model_package_id="mpkg_base",
            module_id="module_classifier",
            objectives=("classification",),
        ),),
    )


@pytest.mark.asyncio
async def test_bridge_resolves_exact_data_trains_and_publishes_immutable_package(tmp_path):
    source = tmp_path / "mutable" / "module_classifier.onnx"
    source.parent.mkdir()
    source.write_bytes(b"portable-onnx-v1")
    calls = {"train": 0, "export": 0}

    async def resolve(revision):
        assert revision == "dsrev_exact_7"
        return dataset(revision)

    async def train(**arguments):
        calls["train"] += 1
        assert arguments["module_id"] == "module_classifier"
        assert arguments["config"] == '{"epochs":3,"lr":0.01}'
        assert arguments["X_train"] == "[[0.0,1.0],[1.0,0.0]]"
        return {"ok": True, "module_id": arguments["module_id"], "job_id": "job_7"}

    async def status(**arguments):
        return {"job_id": arguments["job_id"], "module_id": "module_classifier",
                "status": "complete"}

    async def stop(**_arguments):
        raise AssertionError("stop must not run")

    async def export(**arguments):
        calls["export"] += 1
        assert arguments == {"module_id": "module_classifier", "register_cap": False}
        return {"ok": True, "module_id": "module_classifier",
                "path": str(source), "opset": 17}

    registry = InMemoryModelPackageRegistry()
    publisher = MLWorkshopONNXPublisher(
        export_call=export, artifact_store=tmp_path / "immutable",
        allowed_export_roots=(source.parent,),
        outputs=(output_binding(),), registry=registry)
    adapter = runtime(MLWorkshopCapabilityBridge(
        dataset_resolver=resolve, train_call=train, status_call=status,
        stop_call=stop, publisher=publisher))

    queued = await adapter.submit(request())
    completed = await adapter.observe(queued.training_run_id)
    assert completed.status == "succeeded"
    package = registry.get(completed.output_model_package_id)
    assert package is not None
    assert package.training_run_id == queued.training_run_id
    artifact = Path(package.artifacts[0].uri.removeprefix("file:///"))
    if not artifact.is_absolute():
        artifact = Path("/") / artifact
    assert artifact.name == hashlib.sha256(b"portable-onnx-v1").hexdigest() + ".onnx"
    assert artifact.read_bytes() == b"portable-onnx-v1"
    source.write_bytes(b"changed-after-publication")
    assert artifact.read_bytes() == b"portable-onnx-v1"
    assert await adapter.observe(queued.training_run_id) == completed
    assert calls == {"train": 1, "export": 1}


@pytest.mark.asyncio
async def test_bridge_fails_before_train_on_dataset_or_hyperparameter_drift(tmp_path):
    train_calls = []

    async def train(**arguments):
        train_calls.append(arguments)
        return {}

    async def unused(**_arguments):
        raise AssertionError("unexpected native call")

    def make_bridge(resolver):
        return runtime(MLWorkshopCapabilityBridge(
            dataset_resolver=resolver, train_call=train, status_call=unused,
            stop_call=unused,
            publisher=MLWorkshopONNXPublisher(
                export_call=unused, artifact_store=tmp_path,
                allowed_export_roots=(tmp_path,),
                outputs=(output_binding(),))))

    async def drift(_revision):
        return dataset("dsrev_latest")

    with pytest.raises(ValueError, match="revision"):
        await make_bridge(drift).submit(request())

    async def exact(_revision):
        return dataset()

    with pytest.raises(ValueError, match="hyperparameter"):
        await make_bridge(exact).submit(request(
            hyperparameters=(("unbounded_native_option", 1),)))
    assert train_calls == []


@pytest.mark.parametrize("bad", [None, True, "1.0", float("nan"), float("inf")])
def test_dataset_rejects_non_numeric_and_non_finite_scalars(bad):
    value = dataset()
    value["X_train"][0][0] = bad
    with pytest.raises(ValueError, match="numeric|finite"):
        MLWorkshopDatasetBatch.from_mapping(value, "dsrev_exact_7")


@pytest.mark.asyncio
async def test_bridge_rejects_status_identity_drift(tmp_path):
    async def resolve(_revision):
        return dataset()

    async def train(**arguments):
        return {"ok": True, "module_id": arguments["module_id"], "job_id": "job_7"}

    async def status(**arguments):
        return {"job_id": arguments["job_id"], "module_id": "other_module",
                "status": "running"}

    async def unused(**_arguments):
        raise AssertionError("unexpected native call")

    adapter = runtime(MLWorkshopCapabilityBridge(
        dataset_resolver=resolve, train_call=train, status_call=status,
        stop_call=unused,
        publisher=MLWorkshopONNXPublisher(
            export_call=unused, artifact_store=tmp_path,
            allowed_export_roots=(tmp_path,),
            outputs=(output_binding(),))))
    queued = await adapter.submit(request())
    with pytest.raises(ValueError, match="module identity"):
        await adapter.observe(queued.training_run_id)


@pytest.mark.asyncio
async def test_bridge_waits_for_terminal_cancellation_acknowledgement(tmp_path):
    statuses = iter(("running", "stopping", "stopped"))

    async def resolve(_revision):
        return dataset()

    async def train(**arguments):
        return {"ok": True, "module_id": arguments["module_id"], "job_id": "job_7"}

    async def status(**arguments):
        return {"job_id": arguments["job_id"], "module_id": "module_classifier",
                "status": next(statuses)}

    async def stop(**arguments):
        return {"ok": True, "job_id": arguments["job_id"], "status": "stopping"}

    async def unused(**_arguments):
        raise AssertionError("unexpected export")

    adapter = runtime(MLWorkshopCapabilityBridge(
        dataset_resolver=resolve, train_call=train, status_call=status,
        stop_call=stop,
        publisher=MLWorkshopONNXPublisher(
            export_call=unused, artifact_store=tmp_path,
            allowed_export_roots=(tmp_path,),
            outputs=(output_binding(),)), poll_interval_seconds=0))
    queued = await adapter.submit(request())
    cancelled = await adapter.cancel(queued.training_run_id)
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_publisher_rejects_module_drift_and_artifact_conflict(tmp_path):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"expected")
    submission = MLWorkshopTrainingSubmission(
        training_run_id="trun_" + "a" * 64,
        training_request_id="treq_a", dataset_revision_id="dsrev_a",
        module_id="module_classifier", objective="classification",
        base_model_package_id="mpkg_base", prompt_package_id="",
        hyperparameters=(),
    )

    async def drift(**_arguments):
        return {"ok": True, "module_id": "other_module", "path": str(source)}

    publisher = MLWorkshopONNXPublisher(
        export_call=drift, artifact_store=tmp_path / "immutable",
        allowed_export_roots=(tmp_path,),
        outputs=(output_binding(),))
    with pytest.raises(ValueError, match="identity drifted"):
        await publisher.publish(submission, "job_7")

    outside = tmp_path.parent / "outside.onnx"
    outside.write_bytes(b"outside")

    async def outside_export(**_arguments):
        return {"ok": True, "module_id": "module_classifier", "path": str(outside)}

    publisher = MLWorkshopONNXPublisher(
        export_call=outside_export, artifact_store=tmp_path / "immutable",
        allowed_export_roots=(tmp_path,), outputs=(output_binding(),))
    with pytest.raises(ValueError, match="outside its allowed roots"):
        await publisher.publish(submission, "job_7")

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    immutable = tmp_path / "immutable" / f"{digest}.onnx"
    immutable.parent.mkdir(exist_ok=True)
    immutable.write_bytes(b"conflict")

    async def export(**_arguments):
        return {"ok": True, "module_id": "module_classifier", "path": str(source)}

    publisher = MLWorkshopONNXPublisher(
        export_call=export, artifact_store=immutable.parent,
        allowed_export_roots=(tmp_path,),
        outputs=(output_binding(),))
    with pytest.raises(ValueError, match="conflict"):
        await publisher.publish(submission, "job_7")
