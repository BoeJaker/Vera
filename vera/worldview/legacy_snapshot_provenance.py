"""Payload-free provenance normalization for legacy Worldview snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MAX_PROVENANCE_VALUE_LENGTH = 256


def legacy_snapshot_provenance(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """Return canonical provenance fields without inventing missing authority.

    Older Chroma entries commonly contain ``content_hash`` but no revision ID.
    Projection-backed entries may use ``source_content_hash``.  The Worldview
    snapshot shape uses ``content_hash`` for both, while preserving the value
    exactly.  Non-string and oversized values are rejected to keep untrusted
    backend metadata out of parity evidence.
    """
    if not isinstance(metadata, Mapping):
        return {"revision_id": "", "content_hash": ""}

    revision_id = metadata.get("revision_id", "")
    content_hash = metadata.get("content_hash", "")
    if not content_hash:
        content_hash = metadata.get("source_content_hash", "")

    def bounded_string(value: Any) -> str:
        if not isinstance(value, str) or len(value) > MAX_PROVENANCE_VALUE_LENGTH:
            return ""
        return value

    return {
        "revision_id": bounded_string(revision_id),
        "content_hash": bounded_string(content_hash),
    }
