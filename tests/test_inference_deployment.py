from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from vera.models import (
    InferenceContractConflict, InferenceDeploymentStoreCorrupt,
    InferenceProviderHealth, ModelDeploymentTarget, ModelTrustPolicy,
    ModelAdmissionReceipt,
    SQLiteInferenceDeploymentRegistry, define_inference_deployment,
    evaluate_model_admission, inference_deployment_from_dict,
    inference_deployment_observation_from_dict)
from tests.test_model_package_store import package

pytestmark = pytest.mark.critical


def admitted(value=None):
    value = value or package()
    target = ModelDeploymentTarget(
        "gpu:a", "inference", "tensor/v1", "tensor/v1", 1_000_000,
        accelerators=(), frameworks=("onnxruntime",), max_opset=17)
    receipt = evaluate_model_admission(value, ModelTrustPolicy("default"), target)
    assert receipt.accepted
    return value, receipt


def deployment(value=None):
    value, receipt = admitted(value)
    return define_inference_deployment(
        value, receipt, provider_id="onnx:a", runtime_kind="onnxruntime",
        runtime_version="1.20.0", placements=("gpu", "local"),
        retry_owner="inference-router")


def health(*, provider_id="onnx:a", state="ready", packages=None,
           observed=100):
    return InferenceProviderHealth(
        provider_id, state, observed, observed + 1_000, "runtime-probe",
        tuple(packages if packages is not None else
              ((deployment().package_id,) if state == "ready" else ())),
        concurrency_limit=4, in_flight=1, queue_depth=2)


def test_definition_binds_admission_artifacts_runtime_placement_and_retry_owner():
    value = deployment()
    assert value.runtime_kind == "onnxruntime"
    assert value.artifact_digests == (("weights", "a" * 64),)
    assert value.placements == ("gpu", "local")
    assert value.retry_owner == "inference-router"
    assert inference_deployment_from_dict(value.to_dict()) == value
    changed = value.to_dict()
    changed["runtime_version"] = "different"
    with pytest.raises(ValueError, match="identity"):
        inference_deployment_from_dict(changed)


def test_definition_rejects_failed_or_foreign_admission():
    value, receipt = admitted()
    foreign, _ = admitted(package(version="2"))
    with pytest.raises(InferenceContractConflict, match="another package"):
        define_inference_deployment(
            foreign, receipt, provider_id="onnx:a", runtime_kind="onnxruntime",
            runtime_version="1", retry_owner="router")
    rejected = evaluate_model_admission(
        value, ModelTrustPolicy("default"), ModelDeploymentTarget(
            "bad", "other", "tensor/v1", "tensor/v1", 1_000_000,
            frameworks=("onnxruntime",), max_opset=17))
    with pytest.raises(InferenceContractConflict, match="accepted"):
        define_inference_deployment(
            value, rejected, provider_id="onnx:a", runtime_kind="onnxruntime",
            runtime_version="1", retry_owner="router")
    forged = ModelAdmissionReceipt(
        value.package_id, rejected.policy, rejected.target, ())
    with pytest.raises(InferenceContractConflict, match="does not match"):
        define_inference_deployment(
            value, forged, provider_id="onnx:a", runtime_kind="onnxruntime",
            runtime_version="1", retry_owner="router")


def test_registry_is_durable_and_observations_are_revision_guarded(tmp_path):
    path = tmp_path / "deployments.sqlite"
    value = deployment()
    store = SQLiteInferenceDeploymentRegistry(path)
    store.register(value)
    store.register(value)
    pending = store.observe(
        value.deployment_id, expected_revision=0, desired_state="active",
        observed_state="pending", observed_at_ms=90)
    ready_health = health(packages=(value.package_id,))
    ready = SQLiteInferenceDeploymentRegistry(path).observe(
        value.deployment_id, expected_revision=1, desired_state="active",
        health=ready_health)
    reopened = SQLiteInferenceDeploymentRegistry(path)
    assert reopened.get(value.deployment_id) == value
    assert reopened.current(value.deployment_id) == ready
    assert reopened.history(value.deployment_id) == (pending, ready)
    assert reopened.observe(
        value.deployment_id, expected_revision=1, desired_state="active",
        health=ready_health) == ready
    assert inference_deployment_observation_from_dict(ready.to_dict()) == ready
    with pytest.raises(InferenceContractConflict, match="revision"):
        reopened.observe(
            value.deployment_id, expected_revision=1, desired_state="active",
            observed_state="pending", observed_at_ms=101)


def test_ready_observation_requires_matching_provider_and_package(tmp_path):
    value = deployment()
    store = SQLiteInferenceDeploymentRegistry(tmp_path / "deployments.sqlite")
    store.register(value)
    with pytest.raises(InferenceContractConflict, match="another provider"):
        store.observe(
            value.deployment_id, expected_revision=0, desired_state="active",
            health=health(provider_id="onnx:b"))
    with pytest.raises(InferenceContractConflict, match="omits"):
        store.observe(
            value.deployment_id, expected_revision=0, desired_state="active",
            health=health(packages=("mpkg_other",)))


def test_failed_and_stop_observations_are_explicit_and_do_not_execute(tmp_path):
    value = deployment()
    store = SQLiteInferenceDeploymentRegistry(tmp_path / "deployments.sqlite")
    store.register(value)
    failed = store.observe(
        value.deployment_id, expected_revision=0, desired_state="active",
        observed_state="failed", failure_code="runtime_load_failed",
        observed_at_ms=50)
    stopped = store.observe(
        value.deployment_id, expected_revision=1, desired_state="stopped",
        observed_state="stopped", observed_at_ms=60)
    assert (failed.observed_state, stopped.observed_state) == ("failed", "stopped")
    with pytest.raises(ValueError, match="failure code"):
        store.observe(
            value.deployment_id, expected_revision=2, desired_state="active",
            observed_state="failed", observed_at_ms=70)


def test_concurrent_observation_compare_and_set_has_one_winner(tmp_path):
    path = tmp_path / "deployments.sqlite"
    value = deployment()
    SQLiteInferenceDeploymentRegistry(path).register(value)

    def attempt(state):
        try:
            return SQLiteInferenceDeploymentRegistry(path).observe(
                value.deployment_id, expected_revision=0, desired_state="active",
                observed_state=state, observed_at_ms=10,
                failure_code="load_failed" if state == "failed" else "")
        except InferenceContractConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("pending", "failed")))
    assert sum(result is not None for result in results) == 1
    assert len(SQLiteInferenceDeploymentRegistry(path).history(
        value.deployment_id)) == 1


def test_corrupt_persisted_definition_or_observation_fails_closed(tmp_path):
    path = tmp_path / "deployments.sqlite"
    value = deployment()
    store = SQLiteInferenceDeploymentRegistry(path)
    store.register(value)
    store.observe(value.deployment_id, expected_revision=0,
                  desired_state="active", observed_state="pending",
                  observed_at_ms=10)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE inference_deployment_observations "
                     "SET observation_json='{}'")
    with pytest.raises(InferenceDeploymentStoreCorrupt, match="observation"):
        store.current(value.deployment_id)
