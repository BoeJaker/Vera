import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from vera.models.model_package import (
    ModelArtifact, ModelCompatibility, ModelPackage, ModelPackageConflict,
    model_package_from_dict)
from vera.models.model_package_store import (
    ModelPackageStoreCorrupt, SQLiteModelPackageRegistry, verify_local_artifact)


pytestmark = pytest.mark.critical


def package(uri="artifact://weights", digest="a" * 64, size=3, version="1"):
    return ModelPackage(
        name="demo", version=version, architecture="linear", format="onnx",
        artifacts=(ModelArtifact("weights", uri, digest, size),),
        compatibility=ModelCompatibility(("inference",), "tensor/v1", "tensor/v1"),
        framework="onnxruntime", opset=17, license="apache-2.0")


def test_package_round_trip_recomputes_and_checks_identity():
    value = package()
    assert model_package_from_dict(value.to_dict()) == value
    forged = value.to_dict()
    forged["version"] = "2"
    with pytest.raises(ModelPackageConflict, match="identity"):
        model_package_from_dict(forged)


def test_sqlite_registry_survives_reopen_and_alias_cas(tmp_path):
    path = tmp_path / "registry.sqlite"
    first, second = package(version="1"), package(version="2")
    store = SQLiteModelPackageRegistry(path)
    store.register(first)
    store.alias("default", first.package_id)
    reopened = SQLiteModelPackageRegistry(path)
    assert reopened.get("default") == first
    reopened.register(second)
    reopened.alias("default", second.package_id, expected_package_id=first.package_id)
    assert reopened.get("default") == second
    with pytest.raises(ModelPackageConflict):
        reopened.alias("default", first.package_id, expected_package_id="")


def test_registration_is_idempotent_and_ordered(tmp_path):
    store = SQLiteModelPackageRegistry(tmp_path / "registry.sqlite")
    values = [package(version="2"), package(version="1")]
    for value in values:
        store.register(value)
        store.register(value)
    assert [item.package_id for item in store.list()] == sorted(v.package_id for v in values)


def test_concurrent_idempotent_registration_preserves_one_package(tmp_path):
    path = tmp_path / "registry.sqlite"
    value = package()
    SQLiteModelPackageRegistry(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: SQLiteModelPackageRegistry(path).register(value), range(24)))
    assert SQLiteModelPackageRegistry(path).list() == (value,)


def test_stored_corruption_is_visible_not_silently_dropped(tmp_path):
    path = tmp_path / "registry.sqlite"
    value = package()
    store = SQLiteModelPackageRegistry(path)
    store.register(value)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE model_packages SET package_json=? WHERE package_id=?",
                     (json.dumps({"package_id": value.package_id}), value.package_id))
    with pytest.raises(ModelPackageStoreCorrupt):
        store.get(value.package_id)


def test_verification_reports_success_without_mutating_file(tmp_path):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"abc")
    before = target.stat().st_mtime_ns
    artifact = ModelArtifact("weights", str(target), hashlib.sha256(b"abc").hexdigest(), 3)
    receipt = verify_local_artifact("mpkg_test", artifact)
    assert receipt.verified and receipt.status == "verified"
    assert target.read_bytes() == b"abc" and target.stat().st_mtime_ns == before


@pytest.mark.parametrize("content,size,digest,status", [
    (None, 3, "a" * 64, "missing"),
    (b"abc", 4, hashlib.sha256(b"abc").hexdigest(), "size_mismatch"),
    (b"abc", 3, "a" * 64, "hash_mismatch"),
])
def test_verification_failures_are_explicit(tmp_path, content, size, digest, status):
    target = tmp_path / "model.onnx"
    if content is not None:
        target.write_bytes(content)
    receipt = verify_local_artifact("mpkg_test", ModelArtifact("weights", str(target), digest, size))
    assert receipt.status == status and not receipt.verified


def test_unsupported_uri_limits_and_cancellation_fail_closed(tmp_path):
    unsupported = verify_local_artifact(
        "mpkg_test", ModelArtifact("weights", "https://example.invalid/model", "a" * 64, 1))
    assert unsupported.status == "unsupported_uri"
    target = tmp_path / "model.onnx"
    target.write_bytes(b"abc")
    artifact = ModelArtifact("weights", str(target), hashlib.sha256(b"abc").hexdigest(), 3)
    assert verify_local_artifact("mpkg_test", artifact, max_bytes=2).status == "limit_exceeded"
    assert verify_local_artifact("mpkg_test", artifact, cancelled=lambda: True).status == "cancelled"
