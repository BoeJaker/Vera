from __future__ import annotations

from copy import deepcopy

import pytest

from vera.fabric.native_memory_adapter import (
    NativeMemoryBinding,
    project_native_memory,
)
from vera.fabric.record_revision import create_record_revision


def authority(*, tombstone=False, parents=()):
    return create_record_revision(
        namespace="memory", record_type="fact",
        record_id="rec_native-memory-1", created_at="2026-08-27T12:05:00Z",
        content=None if tombstone else {"native_memory_id": "legacy-1"},
        policy={"classification": "private"}, parents=parents,
        tombstone=tombstone,
    )


def snapshot(**changes):
    value = {
        "id": "legacy-1", "session_id": "session-1", "trace_id": "trace-1",
        "parent_id": "", "created_at": "2026-08-27T12:00:00Z",
        "updated_at": "2026-08-27T12:01:00Z", "record_type": "fact",
        "source_type": "human", "category": "general", "tags": ["learned"],
        "importance": 0.8, "archived": False, "language": "en",
        "text": "short", "summary": "summary", "full_text": "authoritative view",
        "metadata": {"tenant_id": "spoofed", "password": "secret"},
        "model": "private-model", "source_url": "https://user:pass@example.test",
        "embedding": [0.1], "relations": [{"target": "secret"}],
        "capability": "memory.store", "content_hash": "legacy-short-hash",
    }
    value.update(changes)
    return value


def binding(revision=None):
    return NativeMemoryBinding(
        tenant_id="tenant-a", native_memory_id="legacy-1",
        revision=revision or authority(),
    )


def test_projects_native_snapshot_through_fabric_authority():
    item = project_native_memory(snapshot(), binding=binding())
    assert item.tenant_id == "tenant-a"
    assert item.namespace == "memory"
    assert item.record_id == "rec_native-memory-1"
    assert item.text == "authoritative view"
    assert item.policy == {"classification": "private"}
    assert item.citations[0].locator == {"native_memory_id": "legacy-1"}
    assert item.citations[0].revision_id == item.revision_id
    assert item.source_content_hash.startswith("sha256:")
    assert item.projection_hash != item.source_content_hash
    assert item.updated_at == "2026-08-27T12:05:00Z"


def test_projection_is_deterministic_and_does_not_mutate_snapshot():
    source = snapshot()
    original = deepcopy(source)
    assert project_native_memory(source, binding=binding()) == project_native_memory(
        source, binding=binding())
    assert source == original


def test_arbitrary_native_metadata_and_sensitive_fields_are_not_forwarded():
    item = project_native_memory(snapshot(), binding=binding())
    encoded = str(item.to_dict())
    assert "spoofed" not in encoded
    assert "password" not in encoded
    assert "private-model" not in encoded
    assert "user:pass" not in encoded
    assert "relations" not in encoded
    assert item.metadata["native_memory_id"] == "legacy-1"


@pytest.mark.parametrize("changes, message", [
    ({"id": "other"}, "identity"),
    ({"record_type": "message"}, "record_type"),
    ({"archived": True}, "archive state"),
    ({"archived": "false"}, "boolean"),
    ({"created_at": "naive"}, "timestamp"),
    ({"updated_at": "2026-08-27T11:00:00Z"}, "precede"),
    ({"tags": "learned"}, "tags"),
    ({"importance": "many"}, "numeric"),
    ({"full_text": "", "text": "", "summary": ""}, "no projectable text"),
])
def test_invalid_or_conflicting_native_snapshots_fail_closed(changes, message):
    with pytest.raises(ValueError, match=message):
        project_native_memory(snapshot(**changes), binding=binding())


def test_authority_cannot_predate_native_creation():
    with pytest.raises(ValueError, match="cannot precede"):
        project_native_memory(
            snapshot(created_at="2026-08-27T13:00:00Z"), binding=binding())


def test_tombstone_drops_retained_legacy_text():
    active = authority()
    tombstone = authority(tombstone=True, parents=(active.revision_id,))
    item = project_native_memory(
        snapshot(archived=True, updated_at="2026-08-27T12:06:00Z"),
        binding=binding(tombstone),
    )
    assert item.tombstone is True
    assert item.text == ""


def test_binding_requires_exact_record_revision_and_native_identity():
    with pytest.raises(TypeError, match="RecordRevision"):
        NativeMemoryBinding("tenant-a", "legacy-1", object())
    with pytest.raises(ValueError, match="native_memory_id"):
        NativeMemoryBinding("tenant-a", "", authority())


def test_adapter_does_not_import_native_runtime_module():
    source = __import__(
        "vera.fabric.native_memory_adapter", fromlist=["unused"]
    ).__loader__.get_source("vera.fabric.native_memory_adapter")
    assert "from vera.fabric.memory import" not in source
    assert "import vera.fabric.memory" not in source
