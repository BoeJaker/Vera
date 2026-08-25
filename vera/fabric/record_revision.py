"""Storage-neutral immutable record revisions for the Fabric authority plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


RECORD_REVISION_SCHEMA = "vera.fabric-record-revision/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")


def _canonical(value: Any, field: str, *, max_bytes: int = 1_048_576) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{field} must be finite JSON data") from exc
    # json.dumps accepts integer mapping keys by coercing them. Record contracts
    # do not: the round trip must retain exactly the submitted JSON shape.
    if json.loads(encoded) != value:
        raise ValueError(f"{field} must use JSON string object keys")
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} bytes")
    return encoded


def _object(value: Mapping[str, Any] | None, field: str) -> str:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return _canonical(dict(value), field, max_bytes=65_536)


def _timestamp(value: str, field: str, *, optional: bool = False) -> str:
    value = str(value or "").strip()
    if optional and not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat()
    return normalized.replace("+00:00", "Z")


def _identifier(value: str, field: str, prefix: str = "") -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value) or (prefix and not value.startswith(prefix)):
        raise ValueError(f"invalid {field}")
    return value


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordRevision:
    """An immutable observation; nested values are retained as canonical JSON."""

    record_id: str
    revision_id: str
    namespace: str
    record_type: str
    logical_key: str
    snapshot_id: str
    media_type: str
    content_hash: str
    created_at: str
    content_json: str
    artifact_json: str
    content_schema_json: str
    source_json: str
    provenance_json: str
    policy_json: str
    metadata_json: str
    valid_from: str = ""
    valid_to: str = ""
    tombstone: bool = False
    schema: str = RECORD_REVISION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "logical_key": self.logical_key,
            "snapshot_id": self.snapshot_id,
            "record_type": self.record_type,
            "namespace": self.namespace,
            "content": {
                "media_type": self.media_type,
                "inline": json.loads(self.content_json),
                "artifact": json.loads(self.artifact_json),
                "schema": json.loads(self.content_schema_json),
            },
            "source": json.loads(self.source_json),
            "provenance": json.loads(self.provenance_json),
            "policy": json.loads(self.policy_json),
            "created_at": self.created_at,
            "valid_time": {"from": self.valid_from or None,
                           "to": self.valid_to or None},
            "metadata": json.loads(self.metadata_json),
            "content_hash": self.content_hash,
            "tombstone": self.tombstone,
        }


def create_record_revision(
    *, namespace: str, record_type: str, created_at: str,
    record_id: str = "", logical_key: str = "", snapshot_id: str = "",
    content: Any = None,
    media_type: str = "application/json", artifact: Mapping[str, Any] | None = None,
    content_schema: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    parents: Sequence[str] = (), valid_from: str = "", valid_to: str = "",
    tombstone: bool = False,
) -> RecordRevision:
    """Build an idempotent revision without consulting storage or wall clock."""
    namespace = _identifier(namespace, "namespace")
    record_type = _identifier(record_type, "record_type")
    logical_key = str(logical_key or "").strip()
    if logical_key and len(logical_key) > 512:
        raise ValueError("logical_key is too long")
    snapshot_id = str(snapshot_id or "").strip()
    if snapshot_id:
        snapshot_id = _identifier(snapshot_id, "snapshot_id", "snap_")
    if record_id:
        record_id = _identifier(record_id, "record_id", "rec_")
    elif logical_key:
        record_id = "rec_" + hashlib.sha256(
            f"{namespace}\0{logical_key}".encode("utf-8")).hexdigest()
    else:
        raise ValueError("record_id or logical_key is required")
    media_type = str(media_type or "").strip().lower()
    if not media_type or len(media_type) > 255:
        raise ValueError("invalid media_type")
    created_at = _timestamp(created_at, "created_at")
    valid_from = _timestamp(valid_from, "valid_from", optional=True)
    valid_to = _timestamp(valid_to, "valid_to", optional=True)
    if valid_from and valid_to:
        start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
        if end < start:
            raise ValueError("valid_to cannot precede valid_from")

    parent_ids = [str(item or "").strip() for item in parents]
    if any(not _REVISION_ID.fullmatch(item) for item in parent_ids):
        raise ValueError("invalid parent revision")
    if len(parent_ids) > 64:
        raise ValueError("at most 64 parent revisions are allowed")
    if len(parent_ids) != len(set(parent_ids)):
        raise ValueError("parent revisions must be unique")
    provenance_value = dict(provenance or {})
    declared_parents = provenance_value.pop("parents", None)
    if declared_parents is not None and list(declared_parents) != parent_ids:
        raise ValueError("provenance parents conflict with parents")
    provenance_value["parents"] = parent_ids

    artifact_json = _object(artifact, "artifact")
    content_schema_json = _object(content_schema, "content_schema")
    if tombstone:
        if content is not None or json.loads(artifact_json):
            raise ValueError("tombstones cannot carry content or artifacts")
        if not parent_ids:
            raise ValueError("tombstones require a parent revision")
    elif content is None and not json.loads(artifact_json):
        raise ValueError("content or artifact is required")
    content_json = _canonical(content, "content")
    content_hash = _sha256(content_json if content is not None else artifact_json)
    source_json = _object(source, "source")
    provenance_json = _object(provenance_value, "provenance")
    policy_json = _object(policy, "policy")
    metadata_json = _object(metadata, "metadata")

    identity = {
        "schema": RECORD_REVISION_SCHEMA, "record_id": record_id,
        "namespace": namespace, "record_type": record_type,
        "logical_key": logical_key, "snapshot_id": snapshot_id,
        "media_type": media_type,
        "content_hash": content_hash, "created_at": created_at,
        "content": json.loads(content_json), "artifact": json.loads(artifact_json),
        "content_schema": json.loads(content_schema_json),
        "source": json.loads(source_json), "provenance": json.loads(provenance_json),
        "policy": json.loads(policy_json), "metadata": json.loads(metadata_json),
        "valid_from": valid_from, "valid_to": valid_to, "tombstone": tombstone,
    }
    revision_id = "rev_" + hashlib.sha256(
        _canonical(identity, "revision").encode("utf-8")).hexdigest()
    return RecordRevision(
        record_id=record_id, revision_id=revision_id, namespace=namespace,
        record_type=record_type, logical_key=logical_key, snapshot_id=snapshot_id,
        media_type=media_type,
        content_hash=content_hash, created_at=created_at, content_json=content_json,
        artifact_json=artifact_json, content_schema_json=content_schema_json,
        source_json=source_json,
        provenance_json=provenance_json, policy_json=policy_json,
        metadata_json=metadata_json, valid_from=valid_from, valid_to=valid_to,
        tombstone=tombstone,
    )
