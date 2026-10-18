"""Preview-first projection of legacy vectors into canonical JEPA corpora."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..fabric.record_revision import RecordRevision, create_record_revision


SCHEMA = "vera.worldview-canonical-corpus-plan/v1"
_DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class CorpusCandidate:
    source_record_id: str
    revision: RecordRevision
    document: str
    embedding: Sequence[float]
    embedding_hash: str
    tags_json: str


def _embedding_hash(value: Sequence[float]) -> str:
    # JSON is slower than raw ndarray bytes but makes the identity independent
    # of numpy availability and input container/dtype.
    encoded = json.dumps(
        [float(item) for item in value], allow_nan=False,
        separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_candidates(
    *, source_dataset_id: str, target_dataset_id: str,
    ids: Sequence[Any], documents: Sequence[Any],
    metadatas: Sequence[Any], embeddings: Sequence[Any],
) -> tuple[list[CorpusCandidate], dict[str, int]]:
    """Build deterministic candidates only where a source vector is reusable."""
    source_dataset_id = str(source_dataset_id or "").strip()
    target_dataset_id = str(target_dataset_id or "").strip()
    if not _DATASET.fullmatch(source_dataset_id):
        raise ValueError("invalid source_dataset_id")
    if not _DATASET.fullmatch(target_dataset_id):
        raise ValueError("invalid target_dataset_id")
    if source_dataset_id == target_dataset_id:
        raise ValueError("target_dataset_id must differ from source_dataset_id")

    skipped = {
        "missing_document": 0,
        "missing_embedding": 0,
        "invalid_created_at": 0,
        "invalid_source_id": 0,
        "invalid_embedding": 0,
    }
    candidates: list[CorpusCandidate] = []
    for index, raw_id in enumerate(ids):
        source_id = str(raw_id or "").strip()
        if not source_id or len(source_id) > 512:
            skipped["invalid_source_id"] += 1
            continue
        document = str(documents[index] or "") if index < len(documents) else ""
        if not document:
            skipped["missing_document"] += 1
            continue
        embedding = embeddings[index] if index < len(embeddings) else None
        try:
            if embedding is None or len(embedding) < 10:
                skipped["missing_embedding"] += 1
                continue
        except TypeError:
            skipped["missing_embedding"] += 1
            continue
        metadata = metadatas[index] if index < len(metadatas) else {}
        metadata = metadata if isinstance(metadata, Mapping) else {}
        created_at = str(metadata.get("created_at") or "").strip()
        tags = metadata.get("tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (TypeError, ValueError):
                tags = []
        if not isinstance(tags, list):
            tags = []
        try:
            revision = create_record_revision(
                namespace=target_dataset_id,
                record_type="jepa_corpus_item",
                created_at=created_at,
                logical_key=f"{source_dataset_id}:{source_id}",
                content=document,
                media_type="text/plain",
                source={"dataset_id": source_dataset_id, "record_id": source_id},
                metadata={"tags": tags, "source_dataset_id": source_dataset_id,
                          "source_record_id": source_id},
            )
        except ValueError as exc:
            if "created_at" in str(exc):
                skipped["invalid_created_at"] += 1
                continue
            raise
        try:
            emb_hash = _embedding_hash(embedding)
        except (TypeError, ValueError, OverflowError):
            skipped["invalid_embedding"] += 1
            continue
        tags_json = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
        if len(tags_json) > 2000:
            tags_json = "[]"
        candidates.append(CorpusCandidate(
            source_record_id=source_id,
            revision=revision,
            document=document,
            embedding=embedding,
            embedding_hash=emb_hash,
            tags_json=tags_json,
        ))
    candidates.sort(key=lambda item: item.revision.record_id)
    return candidates, skipped


def plan_summary(*, source_dataset_id: str, target_dataset_id: str,
                 requested: int, candidates: Iterable[CorpusCandidate],
                 skipped: Mapping[str, int]) -> dict[str, Any]:
    candidates = list(candidates)
    identity = {
        "schema": SCHEMA,
        "source_dataset_id": source_dataset_id,
        "target_dataset_id": target_dataset_id,
        "items": [{
            "record_id": item.revision.record_id,
            "revision_id": item.revision.revision_id,
            "content_hash": item.revision.content_hash,
            "embedding_hash": item.embedding_hash,
        } for item in candidates],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return {
        "schema": SCHEMA,
        "source_dataset_id": source_dataset_id,
        "target_dataset_id": target_dataset_id,
        "requested": int(requested),
        "eligible_records": len(candidates),
        "ready_to_materialize": bool(candidates),
        "skipped": {key: int(value) for key, value in sorted(skipped.items())},
        "plan_digest": "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "mutates": False,
        "retains_source": True,
    }


def materialize(
    *, candidates: Iterable[CorpusCandidate], expected_digest: str,
    summary: Mapping[str, Any], revision_path: Any, actor: str,
    chroma_upsert: Callable[..., Any], occurred_at: Callable[[], str],
) -> dict[str, Any]:
    """Commit exact candidates and their Chroma projections.

    The caller must rebuild the source plan immediately before this function.
    That makes ``expected_digest`` a compare-and-swap guard over source text,
    vectors and canonical identities.
    """
    if str(expected_digest or "") != str(summary.get("plan_digest") or ""):
        raise ValueError("source plan changed")
    candidates = list(candidates)
    if not candidates:
        raise ValueError("source plan has no eligible records")
    applied = existing = failed = 0
    errors: dict[str, int] = {}
    for item in candidates:
        try:
            revision_path.put(
                item.revision, actor=actor, projections=["chroma"])
            receipt = revision_path.store.receipt(
                item.revision.revision_id, "chroma")
            if receipt["state"] == "applied":
                existing += 1
                continue
            from_state = receipt["state"]
            if from_state in {"failed", "stale", "removed"}:
                revision_path.store.transition(
                    item.revision.revision_id, "chroma",
                    from_state=from_state, to_state="rebuilding",
                    occurred_at=occurred_at())
                from_state = "rebuilding"
            if from_state not in {"pending", "rebuilding"}:
                failed += 1
                errors[from_state] = errors.get(from_state, 0) + 1
                continue
            chroma_upsert(
                ids=[item.revision.record_id], documents=[item.document],
                embeddings=[item.embedding], metadatas=[{
                    "dataset_id": item.revision.namespace,
                    "source": "canonical_revision",
                    "created_at": item.revision.created_at,
                    "tags": item.tags_json,
                    "content_hash": item.revision.content_hash,
                    "revision_id": item.revision.revision_id,
                    "source_dataset_id": summary["source_dataset_id"],
                    "source_record_id": item.source_record_id,
                }])
            revision_path.store.transition(
                item.revision.revision_id, "chroma", from_state=from_state,
                to_state="applied", occurred_at=occurred_at(),
                projection_revision=f"chroma:{item.revision.revision_id}")
            applied += 1
        except Exception as exc:
            failed += 1
            code = re.sub(r"[^a-z0-9._-]", "_", type(exc).__name__.lower())[:128]
            errors[code or "projection_failed"] = errors.get(
                code or "projection_failed", 0) + 1
            try:
                receipt = revision_path.store.receipt(
                    item.revision.revision_id, "chroma")
                if receipt["state"] in {"pending", "rebuilding"}:
                    revision_path.store.transition(
                        item.revision.revision_id, "chroma",
                        from_state=receipt["state"],
                        to_state="failed", occurred_at=occurred_at(),
                        error_code=code or "projection_failed")
            except Exception:
                pass
    return {
        "ok": failed == 0,
        "applied": applied,
        "existing": existing,
        "failed": failed,
        "errors": errors,
        "plan_digest": summary["plan_digest"],
        "target_dataset_id": summary["target_dataset_id"],
        "retains_source": True,
    }
