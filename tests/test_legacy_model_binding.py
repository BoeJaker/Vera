from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from vera.models.legacy_binding import (
    LegacyModelCapabilityBinding, legacy_onnx_bindings)
from vera.models.model_package import ModelPackageConflict
from vera.models.model_package_store import (
    ModelPackageStoreCorrupt, SQLiteModelPackageRegistry)
from tests.test_model_package_store import package


pytestmark = pytest.mark.critical


def populated(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = SQLiteModelPackageRegistry(path)
    first, second = package(version="1"), package(version="2")
    store.register(first)
    store.register(second)
    return path, store, first, second


def binding(package_id, *, cap="ml.onnx.run", selector="demo"):
    return LegacyModelCapabilityBinding(
        capability=cap, selector=selector, package_id=package_id,
        source="ml-onnx-manifest/v1")


def test_generic_legacy_capability_selector_round_trips_after_reopen(tmp_path):
    path, store, first, _ = populated(tmp_path)
    value = binding(first.package_id)
    assert store.bind_legacy_capability(value) == value
    assert SQLiteModelPackageRegistry(path).resolve_legacy_capability(
        "ml.onnx.run", "demo") == value
    assert value.legacy_identity == "ml.onnx.run#demo"


def test_dynamic_per_model_capability_uses_empty_selector(tmp_path):
    _, store, first, _ = populated(tmp_path)
    value = binding(first.package_id, cap="ml.onnx.model.demo", selector="")
    store.bind_legacy_capability(value)
    assert store.resolve_legacy_capability("ml.onnx.model.demo") == value
    assert value.legacy_identity == "ml.onnx.model.demo"


def test_binding_is_idempotent_and_cas_updates(tmp_path):
    _, store, first, second = populated(tmp_path)
    old, new = binding(first.package_id), binding(second.package_id)
    store.bind_legacy_capability(old)
    assert store.bind_legacy_capability(old) == old
    store.bind_legacy_capability(new, expected_package_id=first.package_id)
    assert store.resolve_legacy_capability("ml.onnx.run", "demo") == new
    with pytest.raises(ModelPackageConflict, match="compare-and-set"):
        store.bind_legacy_capability(old, expected_package_id="")


def test_unregistered_package_and_malformed_identity_fail_closed(tmp_path):
    _, store, _, _ = populated(tmp_path)
    with pytest.raises(KeyError, match="not registered"):
        store.bind_legacy_capability(binding("mpkg_" + "a" * 64))
    with pytest.raises(ValueError, match="legacy capability"):
        binding("mpkg_" + "a" * 64, cap="bad capability")
    assert store.legacy_capability_bindings() == ()


def test_concurrent_initial_bind_has_one_winner(tmp_path):
    path, _, first, second = populated(tmp_path)

    def attempt(value):
        try:
            return SQLiteModelPackageRegistry(path).bind_legacy_capability(value)
        except ModelPackageConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [binding(first.package_id), binding(second.package_id)]))
    assert sum(item is not None for item in results) == 1
    assert len(SQLiteModelPackageRegistry(path).legacy_capability_bindings()) == 1


def test_corrupt_binding_is_visible(tmp_path):
    path, store, first, _ = populated(tmp_path)
    store.bind_legacy_capability(binding(first.package_id))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE legacy_model_capability_bindings SET capability='bad value'")
    with pytest.raises(ModelPackageStoreCorrupt, match="legacy capability binding"):
        SQLiteModelPackageRegistry(path).legacy_capability_bindings()


def test_binding_never_reads_or_executes_artifact_uri(tmp_path):
    _, store, first, _ = populated(tmp_path)
    value = binding(first.package_id)
    store.bind_legacy_capability(value)
    assert store.resolve_legacy_capability(value.capability, value.selector) == value


def test_onnx_legacy_identities_bind_as_one_group(tmp_path):
    path, store, first, _ = populated(tmp_path)
    values = legacy_onnx_bindings(first.package_id, "demo")
    assert store.bind_legacy_capabilities(values) == values
    reopened = SQLiteModelPackageRegistry(path)
    assert reopened.resolve_legacy_capability("ml.onnx.run", "demo") == values[0]
    assert reopened.resolve_legacy_capability("ml.onnx.model.demo") == values[1]


def test_group_conflict_leaves_every_identity_unchanged(tmp_path):
    _, store, first, second = populated(tmp_path)
    old = legacy_onnx_bindings(first.package_id, "demo")
    store.bind_legacy_capability(old[1])

    with pytest.raises(ModelPackageConflict, match="compare-and-set"):
        store.bind_legacy_capabilities(legacy_onnx_bindings(second.package_id, "demo"))

    assert store.resolve_legacy_capability("ml.onnx.run", "demo") is None
    assert store.resolve_legacy_capability("ml.onnx.model.demo") == old[1]
