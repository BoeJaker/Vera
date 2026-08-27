"""Read-only compatibility projection for Vera's native memory records.

This module intentionally does not import :mod:`vera.fabric.memory`: importing
that runtime module registers capabilities and probes live backends.  Callers
must pass a plain snapshot (normally ``MemoryRecord.to_dict()``) together with
an explicit, trusted Fabric revision binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from vera.fabric.memory_provider import MemoryCitation, MemoryProjection
from vera.fabric.record_revision import RecordRevision


@dataclass(frozen=True)
class NativeMemoryBinding:
    """Trusted authority needed to project one legacy native-memory identity."""

    tenant_id: str
    native_memory_id: str
    revision: RecordRevision

    def __post_init__(self) -> None:
        if not isinstance(self.revision, RecordRevision):
            raise TypeError("revision must be a RecordRevision")
        native_id = str(self.native_memory_id or "").strip()
        if not native_id or len(native_id) > 256:
            raise ValueError("invalid native_memory_id")
        object.__setattr__(self, "native_memory_id", native_id)


def _snapshot_value(snapshot: Mapping[str, Any], name: str, default: Any = "") -> Any:
    value = snapshot.get(name, default)
    if value is None:
        return default
    return value


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"native {field} must be text")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"native {field} exceeds size limit")
    return value


def _instant(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"native {field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"native {field} must include a timezone")
    return parsed


def _safe_metadata(snapshot: Mapping[str, Any], native_id: str) -> dict[str, Any]:
    """Copy only bounded, non-secret compatibility fields.

    Arbitrary native metadata, vectors, relations, model names and source URLs
    are deliberately excluded.  They may contain credentials or payloads and
    are not authority for portable policy or provenance.
    """
    result: dict[str, Any] = {"native_memory_id": native_id}
    for field in ("trace_id", "parent_id", "source_type", "category",
                  "language", "capability", "content_hash"):
        value = _bounded_text(_snapshot_value(snapshot, field), field, 512).strip()
        if value:
            result[f"native_{field}"] = value
    return result


def project_native_memory(
    snapshot: Mapping[str, Any], *, binding: NativeMemoryBinding,
) -> MemoryProjection:
    """Project a native memory snapshot through explicit Fabric authority.

    The operation is deterministic and read-only.  Tenant, namespace, record
    identity, revision identity, content authority, policy and tombstone state
    come only from ``binding`` and its immutable ``RecordRevision``.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    native_id = _bounded_text(_snapshot_value(snapshot, "id"), "id", 256).strip()
    if not native_id or native_id != binding.native_memory_id:
        raise ValueError("native memory identity does not match binding")

    revision = binding.revision
    native_type = _bounded_text(
        _snapshot_value(snapshot, "record_type", "message"), "record_type", 256
    ).strip()
    if native_type != revision.record_type:
        raise ValueError("native record_type does not match authoritative revision")
    archived = _snapshot_value(snapshot, "archived", False)
    if not isinstance(archived, bool):
        raise ValueError("native archived must be boolean")
    if archived != revision.tombstone:
        raise ValueError("native archive state does not match authoritative tombstone")

    created_at = _bounded_text(_snapshot_value(snapshot, "created_at"), "created_at", 64)
    updated_at = _bounded_text(
        _snapshot_value(snapshot, "updated_at", created_at), "updated_at", 64
    )
    created = _instant(created_at, "created_at")
    updated = _instant(updated_at, "updated_at")
    authority_created = _instant(revision.created_at, "revision.created_at")
    if updated < created:
        raise ValueError("native updated_at cannot precede created_at")
    if authority_created < created:
        raise ValueError("authoritative revision cannot precede native creation")
    projection_updated = max(updated, authority_created).isoformat()

    if archived:
        text = ""
    else:
        text = _bounded_text(
            _snapshot_value(snapshot, "full_text")
            or _snapshot_value(snapshot, "text")
            or _snapshot_value(snapshot, "summary"),
            "text", 65_536,
        )
        if not text:
            raise ValueError("active native memory has no projectable text")

    raw_tags = _snapshot_value(snapshot, "tags", [])
    if not isinstance(raw_tags, (list, tuple)):
        raise ValueError("native tags must be a list")
    session_id = _bounded_text(_snapshot_value(snapshot, "session_id"), "session_id", 256)
    try:
        importance = float(_snapshot_value(snapshot, "importance", 0.5))
    except (TypeError, ValueError) as exc:
        raise ValueError("native importance must be numeric") from exc

    citation_seed = json.dumps(
        [revision.record_id, revision.revision_id, native_id],
        separators=(",", ":"), ensure_ascii=False,
    )
    citation = MemoryCitation.create(
        citation_id="cit_" + hashlib.sha256(citation_seed.encode()).hexdigest(),
        uri=(f"fabric://records/{revision.record_id}"
             f"?revision={revision.revision_id}"),
        record_id=revision.record_id,
        revision_id=revision.revision_id,
        label="Vera native memory projection",
        locator={"native_memory_id": native_id},
    )
    return MemoryProjection(
        tenant_id=binding.tenant_id,
        namespace=revision.namespace,
        record_id=revision.record_id,
        revision_id=revision.revision_id,
        source_content_hash=revision.content_hash,
        record_type=revision.record_type,
        created_at=created_at,
        updated_at=projection_updated,
        session_id=session_id,
        importance=importance,
        tombstone=revision.tombstone,
        text=text,
        citations=(citation,),
        tags=raw_tags,
        policy=json.loads(revision.policy_json),
        metadata=_safe_metadata(snapshot, native_id),
    )
