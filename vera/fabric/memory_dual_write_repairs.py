"""Content-verified adapters for Memory dual-write reconciliation targets."""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping, Any

from .memory_dual_write_runtime import (
    native_memory_content_hash,
    revision_from_native_memory_snapshot,
)
from .memory_dual_write_receipt import SCHEMA as RECEIPT_SCHEMA
from .record_revision import RecordRevision


NativeSnapshotLoader = Callable[[str], Awaitable[Mapping[str, Any]]]
RevisionCommitter = Callable[[RecordRevision, str], Awaitable[Mapping[str, Any]]]
RevisionLoader = Callable[[str, str], Awaitable[RecordRevision]]
BackendWriter = Callable[[Mapping[str, Any], str], Awaitable[Mapping[str, Any]]]
NATIVE_TARGETS = ("postgres", "chroma", "neo4j")


def _repair_evidence(
    target: str, evidence: Mapping[str, Any], idempotency_key: str,
) -> None:
    if not isinstance(evidence, Mapping):
        raise TypeError("reconciliation evidence must be a mapping")
    if evidence.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("unsupported reconciliation evidence schema")
    if evidence.get("authority_switch_allowed") is not False:
        raise ValueError("repair evidence must not grant authority")
    for field in (
        "operation_id", "native_memory_id", "native_content_hash",
        "record_id", "revision_id", "content_hash",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field]:
            raise ValueError(f"repair evidence requires {field}")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("repair requires a stable idempotency key")
    if target == "revision":
        native = evidence.get("native_outcomes")
        if not isinstance(native, Mapping) or native.get("postgres") != "committed":
            raise ValueError(
                "canonical repair requires committed native Postgres authority")


def _exact_commit_result(
    result: Mapping[str, Any], *, target: str, native_id: str,
    record_id: str, revision_id: str, idempotency_key: str,
) -> None:
    if not isinstance(result, Mapping):
        raise TypeError("repair callback must return commit evidence")
    expected = {
        "target": target,
        "native_memory_id": native_id,
        "record_id": record_id,
        "revision_id": revision_id,
        "idempotency_key": idempotency_key,
    }
    if result.get("outcome") != "committed" or any(
            result.get(key) != value for key, value in expected.items()):
        raise ValueError("repair did not return exact commit evidence")


def _verify_revision(revision: RecordRevision, evidence: Mapping[str, Any]) -> None:
    if not isinstance(revision, RecordRevision):
        raise TypeError("revision loader must return a RecordRevision")
    if (revision.record_id != evidence.get("record_id")
            or revision.revision_id != evidence.get("revision_id")
            or revision.content_hash != evidence.get("content_hash")):
        raise ValueError("revision identity does not match reconciliation evidence")


def native_snapshot_from_revision(
    revision: RecordRevision, *, native_memory_id: str,
) -> dict[str, Any]:
    """Reconstruct the public native-memory input represented by a revision."""
    if revision.namespace != "memory.native" or revision.record_type != "memory":
        raise ValueError("revision is not a native-memory authority record")
    content = json.loads(revision.content_json)
    source = json.loads(revision.source_json)
    schema = json.loads(revision.content_schema_json)
    metadata = json.loads(revision.metadata_json)
    if (not isinstance(content, dict) or not isinstance(source, dict)
            or not isinstance(schema, dict) or not isinstance(metadata, dict)):
        raise ValueError("memory revision content or source is invalid")
    if (revision.logical_key != native_memory_id
            or source != {
                "kind": "vera.native-memory",
                "native_memory_id": native_memory_id,
            }
            or metadata != {"native_memory_id": native_memory_id}
            or schema != {
                "media_type": "application/vnd.vera.memory+json",
            }):
        raise ValueError("native memory identity does not match revision")
    tags = content.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise ValueError("memory revision tags must be a string list")
    for field in (
        "text", "summary", "native_record_type", "source_type", "category",
        "parent_id", "session_id", "trace_id", "language",
        "model", "capability", "source_url",
    ):
        if not isinstance(content.get(field, ""), str):
            raise ValueError(f"memory revision {field} must be text")
    if (not isinstance(content.get("ttl_seconds", 0), int)
            or isinstance(content.get("ttl_seconds", 0), bool)):
        raise ValueError("memory revision ttl_seconds must be an integer")
    if not isinstance(content.get("importance", 0.5), (int, float)):
        raise ValueError("memory revision importance must be numeric")
    for field in ("archived", "human_text", "ai_output"):
        if not isinstance(content.get(field, False), bool):
            raise ValueError(f"memory revision {field} must be boolean")
    native_metadata = content.get("metadata", {})
    keywords = content.get("keywords", [])
    relations = content.get("relations", [])
    if not isinstance(native_metadata, dict):
        raise ValueError("memory revision metadata must be an object")
    if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
        raise ValueError("memory revision keywords must be a string list")
    if not isinstance(relations, list):
        raise ValueError("memory revision relations must be a list")
    return {
        "id": native_memory_id,
        "session_id": content.get("session_id", ""),
        "trace_id": content.get("trace_id", ""),
        "parent_id": content.get("parent_id", ""),
        "created_at": revision.created_at,
        "updated_at": revision.created_at,
        "ttl_seconds": content.get("ttl_seconds", 0),
        "record_type": content.get("native_record_type", "message"),
        "source_type": content.get("source_type", "human"),
        "category": content.get("category", "general"),
        "tags": list(tags),
        "importance": content.get("importance", 0.5),
        "archived": bool(content.get("archived", False)),
        "language": content.get("language", "en"),
        "text": content.get("text", ""),
        "summary": content.get("summary", ""),
        "full_text": content.get("text", ""),
        "human_text": bool(content.get("human_text", False)),
        "ai_output": bool(content.get("ai_output", False)),
        "model": content.get("model", ""),
        "capability": content.get("capability", ""),
        "source_url": content.get("source_url", ""),
        "content_hash": native_memory_content_hash({"full_text": content.get("text", "")}),
        "metadata": dict(native_metadata), "keywords": list(keywords),
        "embedding": [], "embedding_model": content.get("embedding_model", ""),
        "graph_id": content.get("graph_id", ""), "relations": list(relations),
    }


@dataclass(frozen=True)
class MemoryDualWriteRepairAdapter:
    """Map reconciliation targets to injected, individually verified effects."""

    native_snapshot_loader: NativeSnapshotLoader
    revision_committer: RevisionCommitter
    revision_loader: RevisionLoader
    backend_writers: Mapping[str, BackendWriter]

    def __post_init__(self) -> None:
        if not all(callable(value) for value in (
                self.native_snapshot_loader, self.revision_committer,
                self.revision_loader)):
            raise TypeError("repair loaders and committer must be callable")
        writers = dict(self.backend_writers)
        if not set(writers).issubset(NATIVE_TARGETS) or not all(
                callable(value) for value in writers.values()):
            raise ValueError("backend writers must be callable known targets")
        object.__setattr__(self, "backend_writers", MappingProxyType(writers))

    async def __call__(
        self, target: str, evidence: Mapping[str, Any], idempotency_key: str,
    ) -> str:
        _repair_evidence(target, evidence, idempotency_key)
        native_id = str(evidence.get("native_memory_id") or "")
        if target == "revision":
            snapshot = await self.native_snapshot_loader(native_id)
            if str(snapshot.get("id") or "") != native_id:
                raise ValueError("native snapshot identity does not match evidence")
            if native_memory_content_hash(snapshot) != evidence.get("native_content_hash"):
                raise ValueError("native snapshot content does not match evidence")
            revision = revision_from_native_memory_snapshot(snapshot)
            _verify_revision(revision, evidence)
            result = await self.revision_committer(revision, idempotency_key)
            _exact_commit_result(
                result, target="revision", native_id=native_id,
                record_id=revision.record_id, revision_id=revision.revision_id,
                idempotency_key=idempotency_key)
            return "committed"
        if target not in NATIVE_TARGETS:
            raise ValueError("unsupported memory repair target")
        writer = self.backend_writers.get(target)
        if writer is None:
            raise ValueError("memory backend repair is not configured")
        revision = await self.revision_loader(
            str(evidence.get("record_id") or ""),
            str(evidence.get("revision_id") or ""))
        _verify_revision(revision, evidence)
        snapshot = native_snapshot_from_revision(
            revision, native_memory_id=native_id)
        if snapshot["content_hash"] != evidence.get("native_content_hash"):
            raise ValueError("reconstructed native content does not match evidence")
        result = await writer(snapshot, idempotency_key)
        _exact_commit_result(
            result, target=target, native_id=native_id,
            record_id=revision.record_id, revision_id=revision.revision_id,
            idempotency_key=idempotency_key)
        return "committed"
