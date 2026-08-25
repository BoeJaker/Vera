import pytest

from vera.fabric.record_revision import create_record_revision
from vera.fabric.revision_store import RevisionConflict, RevisionStore


pytestmark = pytest.mark.critical
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:01:00Z"


def rev(value, *, parents=(), tombstone=False):
    return create_record_revision(
        namespace="test.dataset", record_type="document", logical_key="item-1",
        content=None if tombstone else {"value": value}, created_at=T0 if value == 1 else T1,
        parents=parents, tombstone=tombstone)


def test_put_is_atomic_idempotent_and_head_guarded(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    first = rev(1)
    created = store.put(first, projections=["neo4j", "vector"], expected_head="")
    assert created["created"] is True and created["authority_commit"] == "committed"
    assert [r["state"] for r in created["receipts"]] == ["pending", "pending"]
    replay = store.put(first, projections=["vector", "neo4j"], expected_head="wrong")
    assert replay["created"] is False and store.head(first.record_id)["generation"] == 1
    with pytest.raises(RevisionConflict, match="replay differs"):
        store.put(first, projections=["vector"])
    with pytest.raises(RevisionConflict, match="head changed"):
        store.put(rev(2, parents=[first.revision_id]), projections=["vector"],
                  expected_head="rev_" + "f" * 64)
    assert store.head(first.record_id)["revision_id"] == first.revision_id


def test_projection_failure_reconcile_rebuild_and_stale_cas(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    value = rev(1)
    store.put(value, projections=["vector"])
    failed = store.transition(value.revision_id, "vector", from_state="pending",
                              to_state="failed", occurred_at=T1, error_code="backend_down")
    assert failed["state"] == "failed" and failed["error_code"] == "backend_down"
    assert store.reconcile()[0]["revision_id"] == value.revision_id
    rebuilding = store.transition(value.revision_id, "vector", from_state="failed",
                                  to_state="rebuilding", occurred_at=T1)
    assert rebuilding["attempt"] == 1 and rebuilding["generation"] == 1
    applied = store.transition(value.revision_id, "vector", from_state="rebuilding",
                               to_state="applied", occurred_at=T1,
                               projection_revision="index-build-1")
    assert applied["state"] == "applied"
    with pytest.raises(RevisionConflict, match="state changed"):
        store.transition(value.revision_id, "vector", from_state="rebuilding",
                         to_state="failed", occurred_at=T1,
                         error_code="backend_down")


def test_tombstone_delete_propagation_and_authority_rollback(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    first = rev(1)
    store.put(first, projections=["neo4j", "vector"], expected_head="")
    deleted = rev(2, parents=[first.revision_id], tombstone=True)
    store.put(deleted, projections=["neo4j", "vector"], expected_head=first.revision_id)
    for projection in ("neo4j", "vector"):
        receipt = store.transition(deleted.revision_id, projection, from_state="pending",
                                   to_state="removed", occurred_at=T1)
        assert receipt["state"] == "removed"
    restored = store.rollback(first.record_id, expected_head=deleted.revision_id,
                              target_revision=first.revision_id, occurred_at=T1)
    assert restored["revision_id"] == first.revision_id and restored["generation"] == 3
    assert {r["state"] for r in store.receipts(first.revision_id)} == {"rebuilding"}
    with pytest.raises(RevisionConflict, match="head changed"):
        store.rollback(first.record_id, expected_head=deleted.revision_id,
                       target_revision=first.revision_id, occurred_at=T1)


def test_transaction_rolls_back_if_receipt_insert_fails(tmp_path, monkeypatch):
    store = RevisionStore(tmp_path / "fabric.db")
    value = rev(1)
    original = store._event
    def fail(*args, **kwargs):
        raise RuntimeError("injected")
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.put(value, projections=["vector"], expected_head="")
    monkeypatch.setattr(store, "_event", original)
    assert store.head(value.record_id) is None
    assert store.receipts(value.revision_id) == []


def test_invalid_transitions_and_unknown_rollback_target_fail_closed(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    value = rev(1)
    store.put(value, projections=["vector"])
    with pytest.raises(ValueError, match="invalid receipt transition"):
        store.transition(value.revision_id, "vector", from_state="pending",
                         to_state="stale", occurred_at=T1)
    with pytest.raises(KeyError, match="target revision"):
        store.rollback(value.record_id, expected_head=value.revision_id,
                       target_revision="rev_" + "f" * 64, occurred_at=T1)
    assert store.head(value.record_id)["revision_id"] == value.revision_id


def test_transition_evidence_is_required_and_bounded(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    value = rev(1)
    store.put(value, projections=["vector"])
    with pytest.raises(ValueError, match="projection_revision"):
        store.transition(value.revision_id, "vector", from_state="pending",
                         to_state="applied", occurred_at=T1)
    with pytest.raises(ValueError, match="error_code"):
        store.transition(value.revision_id, "vector", from_state="pending",
                         to_state="failed", occurred_at=T1)
    with pytest.raises(ValueError, match="occurred_at"):
        store.transition(value.revision_id, "vector", from_state="pending",
                         to_state="removed", occurred_at="not-a-time")


def test_head_advancement_requires_existing_explicit_lineage(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    first = rev(1)
    store.put(first, projections=["vector"], expected_head="")
    without_lineage = rev(2)
    with pytest.raises(RevisionConflict, match="current head missing"):
        store.put(without_lineage, projections=["vector"],
                  expected_head=first.revision_id)
    missing = create_record_revision(
        namespace="test.dataset", record_type="document", logical_key="item-2",
        content={"value": 1}, created_at=T1,
        parents=["rev_" + "f" * 64])
    with pytest.raises(RevisionConflict, match="parent revision not found"):
        store.put(missing, projections=["vector"], expected_head="")
    assert store.head(first.record_id)["revision_id"] == first.revision_id


def test_store_rejects_non_contract_objects_and_noop_rollback(tmp_path):
    store = RevisionStore(tmp_path / "fabric.db")
    with pytest.raises(TypeError, match="RecordRevision"):
        store.put(object(), projections=["vector"])
    value = rev(1)
    store.put(value, projections=["vector"])
    with pytest.raises(RevisionConflict, match="already head"):
        store.rollback(value.record_id, expected_head=value.revision_id,
                       target_revision=value.revision_id, occurred_at=T1)
