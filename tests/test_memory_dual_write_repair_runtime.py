import asyncio

import pytest

from vera.fabric.memory_dual_write_receipt import create_memory_dual_write_receipt
from vera.fabric.memory_dual_write_repair_runtime import run_manual_memory_reconciliation
from vera.fabric.memory_dual_write_runtime import (
    native_memory_content_hash,
    revision_from_native_memory_snapshot,
)
from vera.fabric.memory_dual_write_store import MemoryDualWriteReceiptStore
from vera.fabric.revision_store import RevisionStore


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"
DONE = "2026-01-01T00:00:01Z"


def run(value):
    return asyncio.run(value)


def snapshot():
    value = {
        "id": "native-1", "created_at": NOW, "updated_at": NOW,
        "session_id": "session-1", "trace_id": "trace-1", "parent_id": "",
        "ttl_seconds": 0, "record_type": "message", "source_type": "human",
        "category": "general", "tags": ["one"], "importance": 0.7,
        "archived": False, "language": "en", "text": "memory",
        "summary": "summary", "full_text": "memory", "human_text": True,
        "ai_output": False, "model": "", "capability": "memory.store",
        "source_url": "", "metadata": {}, "keywords": [], "embedding": [],
        "relations": [],
    }
    value["content_hash"] = native_memory_content_hash(value)
    return value


def env(tmp_path, enabled="1"):
    return {
        "VERA_MEMORY_RECONCILIATION_ENABLED": enabled,
        "FABRIC_REVISION_SQLITE": str(tmp_path / "revisions.sqlite3"),
        "VERA_MEMORY_DUAL_WRITE_RECEIPTS": str(tmp_path / "receipts.sqlite3"),
        "FABRIC_REVISION_POLICY": '{"readers":["codex"],"writers":["codex"]}',
    }


def add_receipt(tmp_path, *, revision_outcome, failed_backend=None):
    value = snapshot()
    revision = revision_from_native_memory_snapshot(value)
    outcomes = {"postgres": "committed", "chroma": "committed", "neo4j": "committed"}
    errors = {}
    if revision_outcome == "failed":
        errors["revision"] = "revision_failed"
    if failed_backend:
        outcomes[failed_backend] = "failed"
        errors[failed_backend] = f"{failed_backend}_failed"
    receipt = create_memory_dual_write_receipt(
        operation_id="memory.store:native-1", native_memory_id="native-1",
        native_content_hash=value["content_hash"], revision=revision,
        observed_at=NOW, revision_outcome=revision_outcome,
        native_outcomes=outcomes, error_codes=errors)
    MemoryDualWriteReceiptStore(tmp_path / "receipts.sqlite3").append(receipt)
    return value, revision


def test_disabled_gate_constructs_no_state_and_calls_no_backend(tmp_path):
    calls = []

    async def load(*args):
        calls.append(args)

    async def write(*args):
        calls.append(args)
        return True

    with pytest.raises(PermissionError, match="disabled"):
        run(run_manual_memory_reconciliation(
            operation_id="op", attempt_id="attempt", actor="codex",
            native_loader=load, native_writer=write,
            environ=env(tmp_path, enabled="0")))
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_manual_binding_reconstructs_and_commits_missing_revision(tmp_path):
    value, revision = add_receipt(tmp_path, revision_outcome="failed")
    calls = []

    async def load(backend, native_id):
        calls.append(("load", backend, native_id))
        return dict(value)

    async def write(*args):
        calls.append(("write",) + args)
        return True

    result = run(run_manual_memory_reconciliation(
        operation_id="memory.store:native-1", attempt_id="attempt-1",
        actor="codex", native_loader=load, native_writer=write,
        environ=env(tmp_path), started_at=NOW, completed_at=DONE))
    assert result["targets"] == ["revision"]
    assert result["converged"] is True
    assert calls == [("load", "postgres", "native-1")]
    stored = RevisionStore(tmp_path / "revisions.sqlite3").revision(revision.revision_id)
    assert stored["revision_id"] == revision.revision_id


@pytest.mark.parametrize("target", ["postgres", "chroma", "neo4j"])
def test_manual_binding_repairs_only_the_exact_native_backend(tmp_path, target):
    value, revision = add_receipt(
        tmp_path, revision_outcome="committed", failed_backend=target)
    RevisionStore(tmp_path / "revisions.sqlite3").put(
        revision, projections=("memory",))
    calls = []

    async def load(*args):
        calls.append(("load",) + args)
        return dict(value)

    async def write(backend, rebuilt):
        calls.append(("write", backend, rebuilt["id"], rebuilt["content_hash"]))
        return True

    result = run(run_manual_memory_reconciliation(
        operation_id="memory.store:native-1", attempt_id="attempt-1",
        actor="codex", native_loader=load, native_writer=write,
        environ=env(tmp_path), started_at=NOW, completed_at=DONE))
    assert result["targets"] == [target]
    assert calls == [
        ("write", target, "native-1", value["content_hash"]),
        ("load", target, "native-1"),
    ]


def test_manual_binding_refuses_unverified_backend_success(tmp_path):
    value, revision = add_receipt(
        tmp_path, revision_outcome="committed", failed_backend="postgres")
    RevisionStore(tmp_path / "revisions.sqlite3").put(
        revision, projections=("memory",))
    writes = []

    async def load(backend, native_id):
        return {**value, "full_text": "different"}

    async def write(backend, rebuilt):
        writes.append((backend, rebuilt["id"]))
        return True

    result = run(run_manual_memory_reconciliation(
        operation_id="memory.store:native-1", attempt_id="attempt-1",
        actor="codex", native_loader=load, native_writer=write,
        environ=env(tmp_path), started_at=NOW, completed_at=DONE))
    assert writes == [("postgres", "native-1")]
    assert result["outcomes"] == {"postgres": "failed"}
    assert result["error_codes"] == {"postgres": "valueerror"}
    assert result["converged"] is False


def test_manual_binding_denies_policy_before_backend_effect(tmp_path):
    value, revision = add_receipt(
        tmp_path, revision_outcome="committed", failed_backend="neo4j")
    RevisionStore(tmp_path / "revisions.sqlite3").put(
        revision, projections=("memory",))
    calls = []

    async def load(*args):
        calls.append(args)

    async def write(*args):
        calls.append(args)
        return True

    denied = env(tmp_path)
    denied["FABRIC_REVISION_POLICY"] = '{"readers":["user"],"writers":["user"]}'
    result = run(run_manual_memory_reconciliation(
        operation_id="memory.store:native-1", attempt_id="attempt-1",
        actor="codex", native_loader=load, native_writer=write,
        environ=denied, started_at=NOW, completed_at=DONE))
    assert result["outcomes"] == {"neo4j": "failed"}
    assert result["error_codes"] == {"neo4j": "revisionaccessdenied"}
    assert calls == []
