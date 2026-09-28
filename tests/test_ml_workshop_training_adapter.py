from __future__ import annotations

import sys
import subprocess

import pytest

from vera.execution.run_projection import ShadowRunRegistry
from vera.execution.run_journal import SqliteRunJournal
from vera.models.ml_workshop_training_adapter import (
    MLWorkshopTrainingBinding,
    MLWorkshopTrainingRuntime,
)
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.models.training_contracts import TrainingRequest, TrainingRuntime


pytestmark = pytest.mark.critical


def request() -> TrainingRequest:
    return TrainingRequest(
        dataset_revision_id="dsrev_exact_7",
        objective="classification",
        base_model_package_id="mpkg_base",
        hyperparameters=(("epochs", 3), ("learning_rate", 0.01)),
    )


def response_for(submission, *, status="queued", native_job_id="job_7", **extra):
    return {
        "training_run_id": submission.training_run_id,
        "training_request_id": submission.training_request_id,
        "dataset_revision_id": submission.dataset_revision_id,
        "native_job_id": native_job_id,
        "status": status,
        **extra,
    }


def package_for(training_run_id: str, *, evaluation_report_ids=()) -> ModelPackage:
    return ModelPackage(
        name="trained_classifier",
        version="1.0",
        architecture="tiny_mlp",
        format="onnx",
        artifacts=(ModelArtifact(
            role="weights", uri="artifact://trained/model.onnx",
            sha256="a" * 64, size_bytes=123,
        ),),
        compatibility=ModelCompatibility(
            tasks=("classification",), input_contract="features.v1",
            output_contract="classes.v1",
        ),
        training_run_id=training_run_id,
        evaluation_report_ids=evaluation_report_ids,
    )


def runtime(submitter, status_reader=None, stopper=None, registry=None):
    async def unused(*_args):
        raise AssertionError("unexpected native call")

    return MLWorkshopTrainingRuntime(
        runtime_id="ml_workshop",
        bindings=(MLWorkshopTrainingBinding(
            base_model_package_id="mpkg_base", module_id="module_classifier",
            objectives=("classification",),
        ),),
        submitter=submitter,
        status_reader=status_reader or unused,
        stopper=stopper or unused,
        registry=registry,
    )


@pytest.mark.asyncio
async def test_runtime_is_structural_and_submit_is_identity_stable_and_idempotent():
    calls = []

    async def submit(submission):
        calls.append(submission)
        return response_for(submission)

    adapter = runtime(submit)
    assert isinstance(adapter, TrainingRuntime)
    assert adapter.profile().capabilities == ("cancel", "observe", "submit")
    first = await adapter.submit(request())
    second = await adapter.submit(request())
    assert first == second
    assert first.status == "queued"
    assert first.training_run_id.startswith("trun_")
    assert len(calls) == 1
    assert calls[0].dataset_revision_id == "dsrev_exact_7"
    assert calls[0].to_dict()["hyperparameters"] == {
        "epochs": 3, "learning_rate": 0.01,
    }


@pytest.mark.asyncio
async def test_submit_rejects_unbound_package_objective_and_identity_drift():
    async def drift(submission):
        return response_for(submission, dataset_revision_id="dsrev_latest")

    adapter = runtime(drift)
    with pytest.raises(ValueError, match="dataset_revision_id"):
        await adapter.submit(request())
    with pytest.raises(ValueError, match="binding"):
        await adapter.submit(TrainingRequest(
            dataset_revision_id="dsrev_exact_7", objective="classification",
            base_model_package_id="mpkg_other",
        ))
    with pytest.raises(ValueError, match="objective"):
        await adapter.submit(TrainingRequest(
            dataset_revision_id="dsrev_exact_7", objective="generation",
            base_model_package_id="mpkg_base",
        ))


@pytest.mark.asyncio
async def test_observe_maps_running_without_manufacturing_terminal_results():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def status(_job):
        return response_for(saved["submission"], status="running")

    adapter = runtime(submit, status)
    queued = await adapter.submit(request())
    running = await adapter.observe(queued.training_run_id)
    assert running.status == "running"
    assert running.output_model_package_id == ""
    evidence = adapter.run_evidence(queued.training_run_id)
    assert evidence["run"]["status"] == "running"


@pytest.mark.asyncio
async def test_native_status_cannot_regress_from_running_to_queued():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission, status="running")

    async def status(_job):
        return response_for(saved["submission"], status="queued")

    adapter = runtime(submit, status)
    running = await adapter.submit(request())
    with pytest.raises(ValueError, match="regressed"):
        await adapter.observe(running.training_run_id)


@pytest.mark.asyncio
async def test_direct_completion_inserts_running_and_requires_valid_bound_package():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def status(_job):
        pkg = package_for(saved["submission"].training_run_id,
                          evaluation_report_ids=("ereport_accuracy",))
        return response_for(saved["submission"], status="complete",
                            output_model_package=pkg.to_dict())

    registry = ShadowRunRegistry()
    adapter = runtime(submit, status, registry=registry)
    queued = await adapter.submit(request())
    complete = await adapter.observe(queued.training_run_id)
    assert complete.status == "succeeded"
    assert complete.output_model_package_id.startswith("mpkg_")
    assert complete.evaluation_report_ids == ("ereport_accuracy",)
    evidence = adapter.run_evidence(queued.training_run_id)
    assert [event["status"] for event in evidence["run"]["events"]] == [
        "queued", "running", "completed",
    ]
    assert evidence["run"]["artifacts"][0]["uri"].startswith("model-package://")
    assert evidence["run"]["artifacts"][1]["uri"] == \
        "evaluation-report://ereport_accuracy"


@pytest.mark.asyncio
@pytest.mark.parametrize("output,error", [
    (None, "missing_output_model_package"),
    ({"schema": "not-a-model-package"}, "invalid_output_model_package"),
])
async def test_complete_without_valid_model_package_fails_closed(output, error):
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def status(_job):
        extra = {} if output is None else {"output_model_package": output}
        return response_for(saved["submission"], status="complete", **extra)

    adapter = runtime(submit, status)
    queued = await adapter.submit(request())
    failed = await adapter.observe(queued.training_run_id)
    assert failed.status == "failed"
    assert failed.error_code == error
    assert failed.output_model_package_id == ""


@pytest.mark.asyncio
async def test_package_for_another_run_is_rejected():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def status(_job):
        return response_for(saved["submission"], status="complete",
                            output_model_package=package_for("trun_other").to_dict())

    adapter = runtime(submit, status)
    queued = await adapter.submit(request())
    failed = await adapter.observe(queued.training_run_id)
    assert (failed.status, failed.error_code) == (
        "failed", "invalid_output_model_package")


@pytest.mark.asyncio
async def test_native_failure_and_cancellation_map_honestly():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def status(_job):
        return response_for(saved["submission"], status="error", error_code="oom")

    async def stop(_job):
        return response_for(saved["submission"], status="stopped")

    failed_adapter = runtime(submit, status)
    failed_run = await failed_adapter.submit(request())
    failed = await failed_adapter.observe(failed_run.training_run_id)
    assert (failed.status, failed.error_code) == ("failed", "oom")

    cancelled_adapter = runtime(submit, stopper=stop)
    cancelled_run = await cancelled_adapter.submit(request())
    cancelled = await cancelled_adapter.cancel(cancelled_run.training_run_id)
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_run_evidence_survives_registry_recovery(tmp_path):
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def stop(_job):
        return response_for(saved["submission"], status="stopped")

    path = tmp_path / "training-runs.sqlite3"
    adapter = runtime(submit, stopper=stop,
                      registry=ShadowRunRegistry(journal=SqliteRunJournal(str(path))))
    queued = await adapter.submit(request())
    await adapter.cancel(queued.training_run_id)

    recovered = ShadowRunRegistry(journal=SqliteRunJournal(str(path)))
    evidence = recovered.get(queued.training_run_id)
    assert evidence["run"]["status"] == "cancelled"
    assert [event["type"] for event in evidence["run"]["events"]] == [
        "training.submitted", "training.cancelled",
    ]


@pytest.mark.asyncio
async def test_observation_rejects_native_job_and_request_spoofing():
    saved = {}

    async def submit(submission):
        saved["submission"] = submission
        return response_for(submission)

    async def spoof(_job):
        return response_for(saved["submission"], status="running",
                            native_job_id="job_attacker")

    adapter = runtime(submit, spoof)
    queued = await adapter.submit(request())
    with pytest.raises(ValueError, match="native job ID"):
        await adapter.observe(queued.training_run_id)


def test_adapter_import_does_not_pull_ml_framework_or_workshop_runtime():
    probe = """
import sys
before = set(sys.modules)
import vera.models.ml_workshop_training_adapter
loaded = set(sys.modules) - before
forbidden = {'numpy', 'torch', 'tensorflow', 'onnxruntime', 'vera.ml_training'}
raise SystemExit(1 if forbidden & loaded else 0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe], check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
