from dataclasses import replace
import sqlite3

import pytest

from vera.models.admission import (
    ModelDeploymentTarget, ModelPackageAdmissionRejected, ModelTrustPolicy,
    evaluate_model_admission)
from vera.models.model_package import ModelCompatibility, ModelPackageConflict
from vera.models.model_package_store import (
    ModelPackageStoreCorrupt, SQLiteModelPackageRegistry)
from tests.test_model_package_store import package


pytestmark = pytest.mark.critical


def target(**changes):
    values = dict(
        target_id="cpu-prod", task="inference", input_contract="tensor/v1",
        output_contract="tensor/v1", available_memory_bytes=4096,
        accelerators=("cpu",), frameworks=("onnxruntime",), max_opset=18)
    values.update(changes)
    return ModelDeploymentTarget(**values)


def signed_package(**changes):
    value = package()
    values = dict(signature="sig:v1", compatibility=ModelCompatibility(
        ("inference",), "tensor/v1", "tensor/v1", min_memory_bytes=1024,
        accelerators=("cpu",)))
    values.update(changes)
    return replace(value, **values)


def policy(**changes):
    values = dict(policy_id="prod-v1", require_signature=True,
                  trusted_signatures=("sig:v1",))
    values.update(changes)
    return ModelTrustPolicy(**values)


def test_accepted_admission_is_stable_and_contains_declared_evidence_only():
    receipt = evaluate_model_admission(signed_package(), policy(), target())
    assert receipt.accepted and receipt.reasons == ()
    assert receipt.admission_id.startswith("madm_")
    assert receipt == evaluate_model_admission(signed_package(), policy(), target())
    assert set(receipt.to_dict()) == {
        "admission_id", "accepted", "schema", "package_id", "policy", "target", "reasons"}


@pytest.mark.parametrize("package_changes,target_changes,policy_changes,reason", [
    ({"compatibility": ModelCompatibility(("embed",), "tensor/v1", "tensor/v1")}, {}, {},
     "task_unsupported"),
    ({}, {"input_contract": "tokens/v1"}, {}, "input_contract_mismatch"),
    ({}, {"output_contract": "labels/v1"}, {}, "output_contract_mismatch"),
    ({}, {"available_memory_bytes": 1}, {}, "memory_insufficient"),
    ({}, {"accelerators": ("cuda",)}, {}, "accelerator_unavailable"),
    ({}, {"frameworks": ("tensorflow",)}, {}, "framework_unsupported"),
    ({}, {"frameworks": ()}, {}, "framework_unsupported"),
    ({}, {"max_opset": 16}, {}, "opset_unsupported"),
    ({}, {"max_opset": None}, {}, "opset_limit_unspecified"),
    ({"signature": ""}, {}, {}, "signature_missing"),
    ({}, {}, {"trusted_signatures": ("sig:other",)}, "signature_untrusted"),
    ({}, {}, {"trusted_signatures": ()}, "signature_untrusted"),
])
def test_each_incompatibility_is_explicit(package_changes, target_changes,
                                          policy_changes, reason):
    receipt = evaluate_model_admission(
        signed_package(**package_changes), policy(**policy_changes),
        target(**target_changes))
    assert not receipt.accepted and reason in receipt.reasons


def test_admitted_activation_persists_evidence_and_is_idempotent(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = SQLiteModelPackageRegistry(path)
    value = signed_package()
    store.register(value)
    first = store.activate_admitted(
        "default", value.package_id, operation_id="deploy-1",
        policy=policy(), target=target())
    retried = SQLiteModelPackageRegistry(path).activate_admitted(
        "default", value.package_id, operation_id="deploy-1",
        policy=policy(), target=target())
    assert retried == first
    assert retried.admission.accepted
    assert store.get("default") == value
    assert SQLiteModelPackageRegistry(path).admission_for_activation(
        "deploy-1") == first.admission
    assert SQLiteModelPackageRegistry(path).admission_history() == (first.admission,)


def test_rejection_leaves_no_alias_or_activation_history(tmp_path):
    store = SQLiteModelPackageRegistry(tmp_path / "registry.sqlite")
    value = signed_package(signature="")
    store.register(value)
    with pytest.raises(ModelPackageAdmissionRejected) as caught:
        store.activate_admitted(
            "default", value.package_id, operation_id="deploy-1",
            policy=policy(), target=target())
    assert caught.value.receipt.reasons == ("signature_missing",)
    assert store.get("default") is None
    assert store.activation_history() == ()


def test_retry_with_different_policy_or_low_level_operation_fails(tmp_path):
    store = SQLiteModelPackageRegistry(tmp_path / "registry.sqlite")
    value = signed_package()
    store.register(value)
    store.activate_admitted("default", value.package_id, operation_id="deploy-1",
                            policy=policy(), target=target())
    with pytest.raises(ModelPackageConflict, match="another request"):
        store.activate_admitted(
            "default", value.package_id, operation_id="deploy-1",
            policy=policy(policy_id="other"), target=target())

    second = signed_package(signature="sig:v2")
    store.register(second)
    store.activate("raw", second.package_id, operation_id="raw-1")
    with pytest.raises(ModelPackageConflict, match="without admission"):
        store.activate_admitted("raw", second.package_id, operation_id="raw-1",
                                policy=policy(require_signature=False), target=target())


def test_admission_insert_failure_rolls_back_activation(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = SQLiteModelPackageRegistry(path)
    value = signed_package()
    store.register(value)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER reject_admission BEFORE INSERT ON "
                     "model_package_admissions BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture"):
        store.activate_admitted("default", value.package_id, operation_id="deploy-1",
                                policy=policy(), target=target())
    assert store.get("default") is None
    assert store.activation_history() == ()


def test_corrupt_persisted_admission_is_visible_on_retry(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = SQLiteModelPackageRegistry(path)
    value = signed_package()
    store.register(value)
    store.activate_admitted("default", value.package_id, operation_id="deploy-1",
                            policy=policy(), target=target())
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE model_package_admissions SET receipt_json='{}'")
    with pytest.raises(ModelPackageStoreCorrupt, match="model admission"):
        SQLiteModelPackageRegistry(path).activate_admitted(
            "default", value.package_id, operation_id="deploy-1",
            policy=policy(), target=target())
