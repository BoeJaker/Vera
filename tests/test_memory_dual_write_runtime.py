import asyncio
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from vera.fabric.memory_dual_write_runtime import (
    default_memory_dual_write_receipt_path,
    default_memory_revision_path,
    revision_from_native_memory_snapshot,
    store_public_memory_with_revision_canary,
)


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def run(value):
    return asyncio.run(value)


def snapshot():
    return {
        "id": "native-1", "created_at": NOW,
        "text": "short", "full_text": "complete memory",
        "summary": "summary", "record_type": "message",
        "source_type": "human", "category": "general",
        "tags": ["one"], "importance": 0.7,
        "human_text": True, "ai_output": False,
        "content_hash": "native:0123456789abcdef",
    }


def paths(tmp_path, enabled="1", policy='{"writers":["codex"]}'):
    return {
        "VERA_MEMORY_REVISION_CANARY": enabled,
        "FABRIC_REVISION_SQLITE": str(tmp_path / "revisions.sqlite3"),
        "VERA_MEMORY_DUAL_WRITE_RECEIPTS": str(tmp_path / "receipts.sqlite3"),
        "FABRIC_REVISION_POLICY": policy,
    }


def test_default_journals_are_shared_and_out_of_tree_across_import_topologies(
        tmp_path, monkeypatch):
    monkeypatch.setenv("VERA_STATE_DIR", str(tmp_path / "state"))
    from Vera.vera.fabric import memory_dual_write_runtime as upper_runtime

    expected_revision = tmp_path / "state" / "fabric" / "record-revisions.sqlite3"
    expected_receipts = tmp_path / "state" / "fabric" / "memory-dual-write-receipts.sqlite3"
    assert default_memory_revision_path() == str(expected_revision)
    assert upper_runtime.default_memory_revision_path() == str(expected_revision)
    assert default_memory_dual_write_receipt_path() == str(expected_receipts)
    assert upper_runtime.default_memory_dual_write_receipt_path() == str(expected_receipts)


def test_compose_persists_and_forwards_the_default_off_canary_contract():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8")
    assert 'VERA_STATE_DIR: "${VERA_STATE_DIR:-/var/lib/vera-state}"' in source
    assert 'FABRIC_REVISION_SQLITE: "${FABRIC_REVISION_SQLITE:-}"' in source
    assert 'VERA_MEMORY_DUAL_WRITE_RECEIPTS: "${VERA_MEMORY_DUAL_WRITE_RECEIPTS:-/var/lib/vera-state/fabric/memory-dual-write-receipts.sqlite3}"' in source
    assert 'VERA_MEMORY_REVISION_CANARY: "${VERA_MEMORY_REVISION_CANARY:-0}"' in source
    assert 'VERA_MEMORY_RECONCILIATION_ENABLED: "${VERA_MEMORY_RECONCILIATION_ENABLED:-0}"' in source
    assert 'FABRIC_REVISION_POLICY: "${FABRIC_REVISION_POLICY:-}"' in source
    assert "- vera-state:/var/lib/vera-state" in source
    assert "  vera-state:" in source


def test_disabled_path_is_exact_native_await_and_constructs_nothing(tmp_path):
    calls = 0
    expected = {"postgres": True}

    async def native():
        nonlocal calls
        calls += 1
        return expected

    result = run(store_public_memory_with_revision_canary(
        {"deliberately": "not a valid snapshot"}, native_write=native,
        actor="not-a-policy-actor", observed_at="not-a-time",
        environ=paths(tmp_path, enabled="0", policy="not-json")))
    assert result is expected
    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_enabled_path_commits_revision_and_receipt_after_native(tmp_path):
    order = []

    async def native():
        order.append("native")
        return {"postgres": True, "chroma": True, "neo4j": True}

    result = run(store_public_memory_with_revision_canary(
        snapshot(), native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path)))
    assert result == {"postgres": True, "chroma": True, "neo4j": True}
    assert type(result) is dict
    assert json.loads(json.dumps(result)) == result
    assert order == ["native"]
    with sqlite3.connect(tmp_path / "revisions.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM fabric_record_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM fabric_projection_receipts").fetchone()[0] == "pending"
    with sqlite3.connect(tmp_path / "receipts.sqlite3") as conn:
        row = conn.execute(
            "SELECT authority_state,requires_reconciliation "
            "FROM memory_dual_write_receipts").fetchone()
    assert row == ("dual_committed", 0)


def test_invalid_canary_setup_fails_open_to_exact_native_write(tmp_path):
    calls = 0
    expected = {"postgres": True}

    async def native():
        nonlocal calls
        calls += 1
        return expected

    result = run(store_public_memory_with_revision_canary(
        snapshot(), native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path, policy="not-json")))
    assert result is expected
    assert calls == 1


def test_enabled_revision_uses_post_native_derived_snapshot(tmp_path):
    value = snapshot()

    async def native():
        value["metadata"] = {"noise": {"class": "low"}, "canonical_id": "canon-1"}
        value["keywords"] = ["derived"]
        value["embedding_model"] = "embed-1"
        value["graph_id"] = "graph-1"
        value["relations"] = [{"to": "other"}]
        return {"postgres": True}

    run(store_public_memory_with_revision_canary(
        value, native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path)))
    with sqlite3.connect(tmp_path / "revisions.sqlite3") as conn:
        envelope = json.loads(conn.execute(
            "SELECT envelope_json FROM fabric_record_revisions").fetchone()[0])
        content = envelope["content"]["inline"]
    assert content["metadata"]["canonical_id"] == "canon-1"
    assert content["keywords"] == ["derived"]
    assert content["embedding_model"] == "embed-1"
    assert content["graph_id"] == "graph-1"
    assert content["relations"] == [{"to": "other"}]


def test_mutable_updated_at_does_not_change_revision_identity():
    first = revision_from_native_memory_snapshot(snapshot())
    changed = snapshot()
    changed["updated_at"] = "2030-01-01T00:00:00Z"
    second = revision_from_native_memory_snapshot(changed)
    assert second.revision_id == first.revision_id


def test_enabled_path_derives_the_pre_native_memory_hash(tmp_path):
    value = snapshot()
    value["content_hash"] = ""

    async def native():
        return {"postgres": True}

    run(store_public_memory_with_revision_canary(
        value, native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path)))
    with sqlite3.connect(tmp_path / "receipts.sqlite3") as conn:
        evidence = conn.execute(
            "SELECT evidence_json FROM memory_dual_write_receipts").fetchone()[0]
    assert '"native_content_hash":"5d8551b49d08723e"' in evidence


def test_enabled_path_preserves_empty_native_content_and_nonfinite_importance(tmp_path):
    value = snapshot()
    value.update(text="", full_text="", summary="", importance=float("nan"),
                 content_hash="")
    calls = 0

    async def native():
        nonlocal calls
        calls += 1
        return {"postgres": True}

    result = run(store_public_memory_with_revision_canary(
        value, native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path)))
    assert calls == 1 and result == {"postgres": True}
    with sqlite3.connect(tmp_path / "revisions.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM fabric_record_revisions").fetchone()[0] == 1


def test_policy_denial_isolated_from_successful_native_write(tmp_path):
    calls = 0

    async def native():
        nonlocal calls
        calls += 1
        return {"postgres": True}

    result = run(store_public_memory_with_revision_canary(
        snapshot(), native_write=native, actor="codex", observed_at=NOW,
        environ=paths(tmp_path, policy='{"writers":["user"]}')))
    assert calls == 1 and result == {"postgres": True}
    with sqlite3.connect(tmp_path / "receipts.sqlite3") as conn:
        row = conn.execute(
            "SELECT authority_state,requires_reconciliation,evidence_json "
            "FROM memory_dual_write_receipts").fetchone()
    assert row[0:2] == ("native_only", 1)
    assert "revision_write_failed" in row[2]


def test_native_cancellation_propagates_and_never_commits_revision(tmp_path):
    async def native():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(store_public_memory_with_revision_canary(
            snapshot(), native_write=native, actor="codex", observed_at=NOW,
            environ=paths(tmp_path)))
    with sqlite3.connect(tmp_path / "revisions.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM fabric_record_revisions").fetchone()[0] == 0
    with sqlite3.connect(tmp_path / "receipts.sqlite3") as conn:
        evidence = conn.execute(
            "SELECT evidence_json FROM memory_dual_write_receipts").fetchone()[0]
    assert "native_cancelled" in evidence


def test_public_memory_store_uses_the_shared_canary_seam(monkeypatch):
    from vera.fabric import memory

    seen = {}

    async def native_store(record):
        seen["record"] = record
        record.content_hash = record.compute_hash()
        return {"postgres": True}

    async def seam(snapshot_value, *, native_write, actor, observed_at,
                   producer_kind):
        seen["snapshot"] = snapshot_value
        seen["actor"] = actor
        seen["observed_at"] = observed_at
        seen["producer_kind"] = producer_kind
        return await native_write()

    monkeypatch.setattr(memory.MEMORY, "_store_native", native_store)
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: False)
    monkeypatch.setattr(
        memory._revision_canary,
        "store_public_memory_with_revision_canary",
        seam,
    )
    result = run(memory.memory_store(text="remember me", session_id="session-1"))
    assert result["backends"] == {"postgres": True}
    assert result["content_hash"] == seen["record"].compute_hash()
    assert seen["snapshot"]["text"] == "remember me"
    assert seen["snapshot"]["session_id"] == "session-1"
    assert seen["actor"] == "user"
    assert seen["observed_at"]
    assert seen["producer_kind"] == "public_capability"


@pytest.mark.parametrize("record,expected", [
    ({"record_type": "message", "source_type": "human",
      "capability": "agent.chat"}, "conversation"),
    ({"record_type": "capability_call", "category": "activity"},
     "capability_activity"),
    ({"record_type": "event", "category": "dag_step"}, "workflow"),
    ({"record_type": "fact", "tags": ["auto_promoted"]},
     "automatic_promotion"),
])
def test_shared_store_classifies_representative_internal_producers(record, expected):
    from vera.fabric import memory

    value = memory.MemoryRecord(full_text="evidence", **record)
    assert memory._memory_producer_kind(value) == expected


def test_shared_store_observes_internal_producer_once(monkeypatch, tmp_path):
    from vera.fabric import memory

    calls = []

    class Backend:
        def __init__(self, name):
            self.name = name

        async def store(self, record):
            calls.append((self.name, record.id))
            return True

    value = memory.HybridMemoryStore()
    for name in ("postgres", "chroma", "neo4j"):
        value.register(Backend(name))
    for key, setting in paths(tmp_path).items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: False)
    monkeypatch.setattr(memory, "MEMORY_AUTO_EMBED", False)
    record = memory.MemoryRecord(
        id="internal-1", record_type="capability_call", source_type="tool",
        full_text="bounded internal activity evidence")

    result = run(value.store(record, producer_kind="capability_activity"))

    assert result == {"postgres": True, "chroma": True, "neo4j": True}
    assert calls == [(name, "internal-1") for name in ("postgres", "chroma", "neo4j")]
    with sqlite3.connect(tmp_path / "receipts.sqlite3") as conn:
        row = conn.execute(
            "SELECT producer_kind,evidence_json FROM memory_dual_write_receipts"
        ).fetchone()
    assert row[0] == "capability_activity"
    assert json.loads(row[1])["producer_kind"] == "capability_activity"


def test_postgres_rows_render_utc_timestamps_without_double_timezone_suffix():
    from vera.fabric.memory import PostgresBackend

    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = PostgresBackend._row_to_record({
        "id": "native-1", "created_at": stamp, "updated_at": stamp,
    })
    assert record.created_at == "2026-01-01T00:00:00Z"
    assert record.updated_at == "2026-01-01T00:00:00Z"


def test_exact_backend_repair_does_not_fan_out(monkeypatch):
    from vera.fabric import memory

    calls = []

    class Backend:
        def __init__(self, name):
            self.name = name

        async def get(self, record_id):
            calls.append(("get", self.name, record_id))
            return memory.MemoryRecord(id=record_id, full_text="memory")

        async def store(self, record):
            calls.append(("store", self.name, record.id))
            return True

    value = memory.HybridMemoryStore()
    value.register(Backend("postgres"))
    value.register(Backend("neo4j"))
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: False)
    loaded = run(value.repair_get("postgres", "native-1"))
    assert loaded.id == "native-1"
    loaded.content_hash = loaded.compute_hash()
    assert run(value.repair_store("neo4j", loaded)) is True
    assert calls == [
        ("get", "postgres", "native-1"),
        ("store", "neo4j", "native-1"),
    ]


def test_exact_backend_repair_rejects_content_drift_before_effect(monkeypatch):
    from vera.fabric import memory

    calls = []

    class Backend:
        name = "postgres"

        async def store(self, record):
            calls.append(record.id)
            return True

    value = memory.HybridMemoryStore()
    value.register(Backend())
    monkeypatch.setattr(memory, "_sbx_write_blocked", lambda: False)
    record = memory.MemoryRecord(
        id="native-1", full_text="memory", content_hash="tampered")
    with pytest.raises(ValueError, match="content hash"):
        run(value.repair_store("postgres", record))
    assert calls == []


def test_manual_reconcile_capability_delegates_with_exact_backend_seams(monkeypatch):
    from vera.fabric import memory

    seen = {}

    async def runtime(**kwargs):
        seen.update(kwargs)
        return {"converged": False, "authority_switch_allowed": False}

    monkeypatch.setattr(
        memory._revision_repair, "run_manual_memory_reconciliation", runtime)
    result = run(memory.memory_dual_write_reconcile(
        operation_id="memory.store:native-1", attempt_id="attempt-1",
        timeout_s=12.0))
    assert result == {"converged": False, "authority_switch_allowed": False}
    assert seen["operation_id"] == "memory.store:native-1"
    assert seen["attempt_id"] == "attempt-1"
    assert seen["timeout_s"] == 12.0
    assert callable(seen["native_loader"])
    assert callable(seen["native_writer"])
