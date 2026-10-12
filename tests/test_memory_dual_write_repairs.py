import asyncio
from dataclasses import replace

import pytest

from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.memory_dual_write_reconciliation import reconcile_memory_dual_write
from vera.fabric.memory_dual_write_repairs import (
    MemoryDualWriteRepairAdapter,
    native_snapshot_from_revision,
)
from vera.fabric.memory_dual_write_runtime import (
    native_memory_content_hash,
    revision_from_native_memory_snapshot,
)
from vera.fabric.memory_dual_write_store import MemoryDualWriteReceiptStore
from vera.fabric.record_revision import record_revision_from_dict


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
DONE = "2026-01-01T00:00:01Z"


def run(value):
    return asyncio.run(value)


def snapshot():
    value = {
        "id": "native-1", "session_id": "session-1", "trace_id": "trace-1",
        "parent_id": "parent-1", "created_at": NOW, "updated_at": NOW,
        "ttl_seconds": 3600, "record_type": "message", "source_type": "human",
        "category": "decision", "tags": ["one", "two"], "importance": 0.8,
        "archived": False, "language": "en", "text": "short",
        "summary": "summary", "full_text": "complete memory",
        "human_text": True, "ai_output": False, "model": "model-1",
        "capability": "memory.store", "source_url": "https://example.invalid/item",
        "metadata": {"noise": {"class": "low"}, "canonical_id": "canon-1"},
        "keywords": ["derived"], "embedding": [],
        "embedding_model": "embed-1", "graph_id": "graph-1",
        "relations": [{"to": "other"}],
    }
    value["content_hash"] = native_memory_content_hash(value)
    return value


def receipt(*, revision_outcome="committed", target="chroma"):
    value = snapshot()
    revision = revision_from_native_memory_snapshot(value)
    outcomes = {"postgres": "committed", "chroma": "committed", "neo4j": "committed"}
    errors = {}
    if revision_outcome != "committed":
        errors["revision"] = "revision_failed"
    if target:
        outcomes[target] = "failed"
        errors[target] = f"{target}_failed"
    return create_memory_dual_write_receipt(
        operation_id="memory.store:native-1", native_memory_id="native-1",
        native_content_hash=value["content_hash"], revision=revision,
        observed_at=NOW, revision_outcome=revision_outcome,
        native_outcomes=outcomes, error_codes=errors), revision


def exact_result(target, revision, key):
    return {
        "outcome": "committed", "target": target,
        "native_memory_id": "native-1", "record_id": revision.record_id,
        "revision_id": revision.revision_id, "idempotency_key": key,
    }


def adapter_for(value, revision, calls, *, result_override=None):
    async def load_native(native_id):
        calls.append(("load-native", native_id))
        return dict(value)

    async def commit_revision(loaded, key):
        calls.append(("revision", key, loaded.revision_id))
        return result_override or exact_result("revision", revision, key)

    async def load_revision(record_id, revision_id):
        calls.append(("load-revision", record_id, revision_id))
        return record_revision_from_dict(revision.to_dict())

    def writer(target):
        async def write(loaded, key):
            calls.append((target, key, loaded["id"]))
            return result_override or exact_result(target, revision, key)
        return write

    return MemoryDualWriteRepairAdapter(
        native_snapshot_loader=load_native,
        revision_committer=commit_revision,
        revision_loader=load_revision,
        backend_writers={target: writer(target) for target in ("postgres", "chroma", "neo4j")},
    )


def test_native_snapshot_round_trip_retains_public_memory_semantics():
    value = snapshot()
    revision = revision_from_native_memory_snapshot(value)
    rebuilt = native_snapshot_from_revision(revision, native_memory_id=value["id"])
    for field in (
        "id", "session_id", "trace_id", "parent_id", "created_at", "updated_at",
        "ttl_seconds", "record_type", "source_type", "category", "tags",
        "importance", "archived", "language", "summary", "human_text",
        "ai_output", "model", "capability", "source_url", "content_hash",
        "metadata", "keywords", "embedding_model", "graph_id", "relations",
    ):
        assert rebuilt[field] == value[field]
    assert rebuilt["text"] == rebuilt["full_text"] == value["full_text"]


def test_revision_repair_verifies_snapshot_and_returns_exact_commit():
    evidence, revision = receipt(revision_outcome="failed", target=None)
    calls = []
    adapter = adapter_for(snapshot(), revision, calls)
    assert run(adapter("revision", evidence.to_dict(), "attempt-1:revision")) == "committed"
    assert calls == [
        ("load-native", "native-1"),
        ("revision", "attempt-1:revision", revision.revision_id),
    ]


@pytest.mark.parametrize("target", ["postgres", "chroma", "neo4j"])
def test_backend_repair_invokes_only_the_requested_writer(target):
    evidence, revision = receipt(target=target)
    calls = []
    adapter = adapter_for(snapshot(), revision, calls)
    key = f"attempt-1:{target}"
    assert run(adapter(target, evidence.to_dict(), key)) == "committed"
    assert calls == [
        ("load-revision", revision.record_id, revision.revision_id),
        (target, key, "native-1"),
    ]


def test_reconciliation_adapter_repairs_exact_split_target_and_converges(tmp_path):
    evidence, revision = receipt(target="chroma")
    store = MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3")
    store.append(evidence)
    calls = []
    adapter = adapter_for(snapshot(), revision, calls)
    result = run(reconcile_memory_dual_write(
        store, operation_id=evidence.operation_id, attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=adapter))
    assert result.targets == ("chroma",)
    assert result.converged is True
    assert [call[0] for call in calls] == ["load-revision", "chroma"]
    replay = run(reconcile_memory_dual_write(
        store, operation_id=evidence.operation_id, attempt_id="attempt-1",
        started_at=NOW, completed_at=DONE, repair=adapter))
    assert replay.outcomes == {"chroma": "committed"}
    assert len(calls) == 2


def test_snapshot_tamper_is_rejected_before_revision_commit():
    evidence, revision = receipt(revision_outcome="failed", target=None)
    value = snapshot()
    value["full_text"] = "tampered"
    calls = []
    adapter = adapter_for(value, revision, calls)
    with pytest.raises(ValueError, match="content does not match"):
        run(adapter("revision", evidence.to_dict(), "attempt-1:revision"))
    assert calls == [("load-native", "native-1")]


def test_revision_repair_requires_committed_postgres_authority():
    evidence, revision = receipt(revision_outcome="failed", target=None)
    raw = evidence.to_dict()
    raw["native_outcomes"]["postgres"] = "failed"
    calls = []
    adapter = adapter_for(snapshot(), revision, calls)
    with pytest.raises(ValueError, match="Postgres authority"):
        run(adapter("revision", raw, "attempt-1:revision"))
    assert calls == []


def test_revision_tamper_is_rejected_before_backend_write():
    evidence, revision = receipt(target="chroma")
    different = revision_from_native_memory_snapshot({**snapshot(), "id": "native-2"})
    calls = []
    adapter = adapter_for(snapshot(), different, calls)
    with pytest.raises(ValueError, match="identity"):
        run(adapter("chroma", evidence.to_dict(), "attempt-1:chroma"))
    assert calls[0][0] == "load-revision"
    assert all(call[0] != "chroma" for call in calls)


def test_revision_schema_tamper_is_rejected_before_backend_write():
    evidence, revision = receipt(target="chroma")
    forged = replace(revision, content_schema_json='{"media_type":"text/plain"}')
    calls = []
    adapter = adapter_for(snapshot(), forged, calls)
    with pytest.raises(ValueError, match="identity"):
        run(adapter("chroma", evidence.to_dict(), "attempt-1:chroma"))
    assert all(call[0] != "chroma" for call in calls)


@pytest.mark.parametrize("change", [
    {"schema": "vera.memory-dual-write-receipt/v999"},
    {"authority_switch_allowed": True},
    {"native_memory_id": ""},
])
def test_invalid_reconciliation_evidence_is_rejected_before_load(change):
    evidence, revision = receipt(target="chroma")
    raw = evidence.to_dict()
    raw.update(change)
    calls = []
    adapter = adapter_for(snapshot(), revision, calls)
    with pytest.raises(ValueError):
        run(adapter("chroma", raw, "attempt-1:chroma"))
    assert calls == []


def test_callback_must_acknowledge_exact_identity_and_idempotency_key():
    evidence, revision = receipt(target="chroma")
    calls = []
    adapter = adapter_for(
        snapshot(), revision, calls,
        result_override={
            **exact_result("chroma", revision, "wrong-key"),
            "idempotency_key": "wrong-key",
        })
    with pytest.raises(ValueError, match="exact commit evidence"):
        run(adapter("chroma", evidence.to_dict(), "attempt-1:chroma"))


def test_backend_writer_mapping_is_immutable_and_unknown_targets_fail_closed():
    evidence, revision = receipt(target="chroma")
    adapter = adapter_for(snapshot(), revision, [])
    with pytest.raises(TypeError):
        adapter.backend_writers["chroma"] = adapter.backend_writers["chroma"]
    with pytest.raises(ValueError, match="unsupported"):
        run(adapter("elastic", evidence.to_dict(), "attempt-1:elastic"))
