from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from vera.models.model_package import ModelPackageConflict
from vera.models.model_package_store import (
    ModelPackageStoreCorrupt, SQLiteModelPackageRegistry)
from tests.test_model_package_store import package


pytestmark = pytest.mark.critical


def populated(tmp_path):
    path = tmp_path / "registry.sqlite"
    store = SQLiteModelPackageRegistry(path)
    first, second, third = package(version="1"), package(version="2"), package(version="3")
    for item in (first, second, third):
        store.register(item)
    return path, store, first, second, third


def test_activation_is_transactional_idempotent_and_survives_reopen(tmp_path):
    path, store, first, second, _ = populated(tmp_path)
    initial = store.activate("default", first.package_id, operation_id="deploy-1")
    changed = store.activate(
        "default", second.package_id, expected_package_id=first.package_id,
        operation_id="deploy-2")
    retried = SQLiteModelPackageRegistry(path).activate(
        "default", second.package_id, expected_package_id=first.package_id,
        operation_id="deploy-2")

    assert retried == changed
    assert initial.sequence < changed.sequence
    assert SQLiteModelPackageRegistry(path).get("default") == second
    assert SQLiteModelPackageRegistry(path).activation_history("default") == (initial, changed)


def test_operation_id_reuse_with_different_intent_fails_closed(tmp_path):
    _, store, first, second, _ = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="same-request")
    with pytest.raises(ModelPackageConflict, match="another request"):
        store.activate("default", second.package_id, operation_id="same-request")
    assert store.get("default") == first


def test_stale_activation_refuses_without_history_or_alias_change(tmp_path):
    _, store, first, second, _ = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")
    with pytest.raises(ModelPackageConflict, match="compare-and-set"):
        store.activate("default", second.package_id, expected_package_id="",
                       operation_id="stale")
    assert store.get("default") == first
    assert [r.operation_id for r in store.activation_history()] == ["deploy-1"]


def test_rollback_restores_previous_alias_and_is_idempotent(tmp_path):
    path, store, first, second, _ = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")
    store.activate("default", second.package_id, expected_package_id=first.package_id,
                   operation_id="deploy-2")
    rollback = store.rollback("deploy-2", operation_id="rollback-2")
    retried = SQLiteModelPackageRegistry(path).rollback(
        "deploy-2", operation_id="rollback-2")

    assert retried == rollback
    assert rollback.previous_package_id == second.package_id
    assert rollback.package_id == first.package_id
    assert store.get("default") == first


def test_rollback_of_initial_activation_removes_alias(tmp_path):
    _, store, first, _, _ = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")
    receipt = store.rollback("deploy-1", operation_id="rollback-1")
    assert receipt.package_id == ""
    assert store.get("default") is None


def test_stale_or_duplicate_rollback_cannot_overwrite_newer_state(tmp_path):
    _, store, first, second, third = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")
    store.activate("default", second.package_id, expected_package_id=first.package_id,
                   operation_id="deploy-2")
    store.activate("default", third.package_id, expected_package_id=second.package_id,
                   operation_id="deploy-3")
    with pytest.raises(ModelPackageConflict, match="compare-and-set"):
        store.rollback("deploy-2", operation_id="stale-rollback")
    assert store.get("default") == third
    assert all(r.operation_id != "stale-rollback" for r in store.activation_history())

    store.rollback("deploy-3", operation_id="rollback-3")
    with pytest.raises(ModelPackageConflict, match="already rolled back"):
        store.rollback("deploy-3", operation_id="different-rollback")
    assert store.get("default") == second


def test_concurrent_compare_and_set_has_one_winner_and_one_receipt(tmp_path):
    path, store, first, second, third = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")

    def attempt(args):
        package_id, operation_id = args
        try:
            return SQLiteModelPackageRegistry(path).activate(
                "default", package_id, expected_package_id=first.package_id,
                operation_id=operation_id)
        except ModelPackageConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [(second.package_id, "deploy-2"),
                                          (third.package_id, "deploy-3")]))
    assert sum(item is not None for item in results) == 1
    assert len(store.activation_history()) == 2


def test_activation_never_reads_artifact_uris(tmp_path):
    _, store, first, _, _ = populated(tmp_path)
    receipt = store.activate("default", first.package_id, operation_id="deploy-1")
    assert receipt.package_id == first.package_id


def test_corrupt_activation_history_is_visible_after_reopen(tmp_path):
    path, store, first, _, _ = populated(tmp_path)
    store.activate("default", first.package_id, operation_id="deploy-1")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE model_package_activations SET operation_id='bad value'")
    with pytest.raises(ModelPackageStoreCorrupt, match="activation receipt"):
        SQLiteModelPackageRegistry(path).activation_history()


def test_receipt_write_failure_rolls_back_alias_movement(tmp_path):
    path, store, first, _, _ = populated(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER reject_activation BEFORE INSERT ON "
                     "model_package_activations BEGIN SELECT RAISE(ABORT, 'fixture'); END")

    with pytest.raises(sqlite3.IntegrityError, match="fixture"):
        store.activate("default", first.package_id, operation_id="deploy-1")

    assert store.get("default") is None
    assert store.activation_history() == ()
