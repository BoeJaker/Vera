"""Fail-closed canonical revision enrichment for JEPA source metadata."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import Any


def enrich_matching_revisions(
    records: MutableMapping[str, MutableMapping[str, Any]],
    resolve_current: Callable[[tuple[str, ...]], Mapping[str, Mapping[str, Any]]],
) -> dict[str, int]:
    """Attach a revision only when its exact content hash is already present.

    A shared record ID is not enough to prove that a mutable Chroma projection
    represents the current canonical revision. This join therefore refuses to
    invent either side of the identity: records without a projection hash stay
    ineligible, and a differing canonical hash is reported as a mismatch.
    """
    counts = {
        "records": 0,
        "already_complete": 0,
        "enriched": 0,
        "missing_content_hash": 0,
        "missing_revision": 0,
        "hash_mismatch": 0,
        "invalid_revision": 0,
        "lookup_error": 0,
    }
    candidates: list[str] = []
    for record_id, metadata in records.items():
        counts["records"] += 1
        if not isinstance(metadata, MutableMapping):
            counts["invalid_revision"] += 1
            continue
        existing_revision = str(metadata.get("revision_id") or "").strip()
        content_hash = str(metadata.get("content_hash") or "").strip()
        if existing_revision and content_hash:
            counts["already_complete"] += 1
            continue
        if not content_hash:
            counts["missing_content_hash"] += 1
            continue
        candidates.append(str(record_id))

    try:
        revisions = resolve_current(tuple(candidates)) if candidates else {}
    except Exception:
        counts["lookup_error"] += len(candidates)
        return counts
    if not isinstance(revisions, Mapping):
        counts["lookup_error"] += len(candidates)
        return counts

    for record_id in candidates:
        metadata = records[record_id]
        content_hash = str(metadata.get("content_hash") or "").strip()
        try:
            revision = revisions.get(record_id)
        except Exception:
            counts["lookup_error"] += 1
            continue
        if revision is None:
            counts["missing_revision"] += 1
            continue
        if not isinstance(revision, Mapping):
            counts["invalid_revision"] += 1
            continue
        revision_id = str(revision.get("revision_id") or "").strip()
        revision_record_id = str(revision.get("record_id") or "").strip()
        revision_hash = str(revision.get("content_hash") or "").strip()
        if not revision_id or revision_record_id != str(record_id) or not revision_hash:
            counts["invalid_revision"] += 1
            continue
        if revision_hash != content_hash:
            counts["hash_mismatch"] += 1
            continue
        metadata["revision_id"] = revision_id
        counts["enriched"] += 1
    return counts
