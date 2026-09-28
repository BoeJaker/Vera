"""Immutable provenance binding for JEPA Worldview retrieval evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ..fabric.dataset_provider import DatasetSnapshot
from ..fabric.retrieval_comparison import RetrievalCitation
from ..models.model_package import (
    ModelArtifact,
    ModelCompatibility,
    ModelPackage,
    model_package_from_dict,
)


SCHEMA = "vera.jepa-retrieval-provenance/v1"
MAX_RECORDS = 20_000
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENT.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _checkpoint_digest(blob: bytes) -> str:
    if not isinstance(blob, bytes) or not blob:
        raise ValueError("checkpoint blob must be non-empty bytes")
    return hashlib.sha256(blob).hexdigest()


def _canonical_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot records must be canonical JSON") from exc


def _validated_snapshot_records(
    snapshot: DatasetSnapshot,
    records: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[RetrievalCitation, ...]]:
    if isinstance(records, (str, bytes)):
        raise TypeError("snapshot records must be a sequence of objects")
    if len(records) > MAX_RECORDS:
        raise ValueError(f"snapshot records exceed {MAX_RECORDS}")
    frozen = tuple(_canonical_json(dict(item)) for item in records
                   if isinstance(item, Mapping))
    if len(frozen) != len(records):
        raise TypeError("snapshot records must be objects")
    recreated, recreated_records = DatasetSnapshot.create(
        dataset_id=snapshot.dataset_id,
        created_at=snapshot.created_at,
        records=frozen,
        schema=snapshot.schema,
        provenance=snapshot.provenance,
    )
    if recreated != snapshot or recreated_records != frozen:
        raise ValueError("records do not reproduce the exact DatasetSnapshot")
    citations: list[RetrievalCitation] = []
    for record in frozen:
        try:
            citations.append(RetrievalCitation(
                record["record_id"], record["revision_id"]))
        except KeyError as exc:
            raise ValueError(
                "every snapshot record requires record_id and revision_id") from exc
    if len(set(citations)) != len(citations):
        raise ValueError("snapshot record/revision citations must be unique")
    if len({item.record_id for item in citations}) != len(citations):
        raise ValueError("snapshot record IDs must be unique")
    return frozen, tuple(sorted(citations))


@dataclass(frozen=True)
class JepaRetrievalProvenance:
    """Bind one checkpoint and exact index membership to one dataset snapshot."""

    snapshot: DatasetSnapshot
    checkpoint: ModelPackage
    provider_revision: str
    citations: tuple[RetrievalCitation, ...]
    indexed_record_ids: tuple[str, ...]
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be DatasetSnapshot")
        if not isinstance(self.checkpoint, ModelPackage):
            raise TypeError("checkpoint must be ModelPackage")
        if self.checkpoint.name != "worldview-jepa":
            raise ValueError("checkpoint must identify JEPA Worldview")
        if self.checkpoint.architecture != "graph-jepa":
            raise ValueError("checkpoint architecture must be graph-jepa")
        metadata = dict(self.checkpoint.metadata)
        if metadata.get("dataset_snapshot_id") != self.snapshot.snapshot_id:
            raise ValueError("checkpoint is not bound to the DatasetSnapshot")
        object.__setattr__(self, "provider_revision", _identifier(
            self.provider_revision, "provider revision"))
        citations = tuple(sorted(self.citations))
        if not citations or len(citations) > MAX_RECORDS or not all(
                isinstance(item, RetrievalCitation) for item in citations):
            raise ValueError("citations must contain bounded RetrievalCitation values")
        if len(set(citations)) != len(citations):
            raise ValueError("citations must be unique")
        if len({item.record_id for item in citations}) != len(citations):
            raise ValueError("citation record IDs must be unique")
        object.__setattr__(self, "citations", citations)
        indexed = tuple(sorted(_identifier(item, "indexed record ID")
                               for item in self.indexed_record_ids))
        if len(set(indexed)) != len(indexed):
            raise ValueError("indexed record IDs must be unique")
        if indexed != tuple(sorted(item.record_id for item in citations)):
            raise ValueError("JEPA index membership must equal snapshot record IDs")
        if len(indexed) != self.snapshot.record_count:
            raise ValueError("JEPA index membership must cover the full snapshot")
        object.__setattr__(self, "indexed_record_ids", indexed)
        if self.schema != SCHEMA:
            raise ValueError("unsupported JEPA retrieval provenance schema")

    @classmethod
    def create(
        cls,
        *,
        snapshot: DatasetSnapshot,
        snapshot_records: Sequence[Mapping[str, Any]],
        checkpoint_blob: bytes,
        indexed_record_ids: Sequence[str],
        provider_revision: str,
        framework_version: str = "",
        training_run_id: str = "",
    ) -> "JepaRetrievalProvenance":
        if not isinstance(snapshot, DatasetSnapshot):
            raise TypeError("snapshot must be DatasetSnapshot")
        _, citations = _validated_snapshot_records(snapshot, snapshot_records)
        digest = _checkpoint_digest(checkpoint_blob)
        checkpoint = ModelPackage(
            name="worldview-jepa",
            version="sha256-" + digest[:16],
            architecture="graph-jepa",
            format="pytorch",
            artifacts=(ModelArtifact(
                role="checkpoint",
                uri=f"fabric-checkpoint:worldview-global:{digest}",
                sha256=digest,
                size_bytes=len(checkpoint_blob),
            ),),
            compatibility=ModelCompatibility(
                tasks=("retrieval", "world-model"),
                input_contract="vera.dataset-snapshot/v1",
                output_contract="vera.worldview-retrieval/v1",
                accelerators=("cpu", "cuda"),
            ),
            framework="pytorch",
            framework_version=str(framework_version or ""),
            source_revision=snapshot.snapshot_id,
            training_run_id=str(training_run_id or ""),
            metadata=(("dataset_snapshot_id", snapshot.snapshot_id),),
        )
        return cls(
            snapshot=snapshot,
            checkpoint=checkpoint,
            provider_revision=provider_revision,
            citations=citations,
            indexed_record_ids=tuple(indexed_record_ids),
        )

    @property
    def checkpoint_sha256(self) -> str:
        return self.checkpoint.artifacts[0].sha256

    def verifies_runtime(self, *, checkpoint_blob: bytes,
                         indexed_record_ids: Sequence[str]) -> bool:
        try:
            digest = _checkpoint_digest(checkpoint_blob)
            indexed = tuple(sorted(_identifier(item, "indexed record ID")
                                   for item in indexed_record_ids))
        except (TypeError, ValueError):
            return False
        return digest == self.checkpoint_sha256 and indexed == self.indexed_record_ids

    def citations_for_result(self, result: Mapping[str, Any], *,
                             limit: int) -> tuple[RetrievalCitation, ...]:
        if not isinstance(result, Mapping) or result.get("error"):
            raise ValueError("a successful JEPA query result is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        rows = result.get("results")
        if not isinstance(rows, (list, tuple)) or len(rows) > 1000:
            raise ValueError("JEPA query results must be a bounded sequence")
        by_record = {item.record_id: item for item in self.citations}
        output: list[RetrievalCitation] = []
        seen: set[RetrievalCitation] = set()
        for row in rows[:limit]:
            if not isinstance(row, Mapping):
                raise ValueError("JEPA query result entries must be objects")
            record_id = _identifier(row.get("id"), "JEPA result record ID")
            citation = by_record.get(record_id)
            if citation is None:
                raise ValueError("JEPA result is outside the bound DatasetSnapshot")
            if citation not in seen:
                output.append(citation)
                seen.add(citation)
        return tuple(output)

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot.snapshot_id,
            "model_package_id": self.checkpoint.package_id,
            "provider_revision": self.provider_revision,
            "record_count": len(self.indexed_record_ids),
            "checkpoint_sha256": self.checkpoint_sha256,
            "authority": "derived_evidence_only",
            "executes": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot": self.snapshot.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "provider_revision": self.provider_revision,
            "citations": [item.to_dict() for item in self.citations],
            "indexed_record_ids": list(self.indexed_record_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JepaRetrievalProvenance":
        if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
            raise ValueError("unsupported JEPA retrieval provenance value")
        try:
            raw_snapshot = value["snapshot"]
            snapshot = DatasetSnapshot(
                dataset_id=raw_snapshot["dataset_id"],
                snapshot_id=raw_snapshot["snapshot_id"],
                created_at=raw_snapshot["created_at"],
                record_count=raw_snapshot["record_count"],
                schema=raw_snapshot["schema"],
                provenance=raw_snapshot["provenance"],
                schema_version=raw_snapshot["schema_version"],
            )
            checkpoint = model_package_from_dict(value["checkpoint"])
            citations = tuple(RetrievalCitation(**item)
                              for item in value["citations"])
            return cls(
                snapshot=snapshot,
                checkpoint=checkpoint,
                provider_revision=value["provider_revision"],
                citations=citations,
                indexed_record_ids=tuple(value["indexed_record_ids"]),
                schema=value["schema"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed JEPA retrieval provenance value") from exc
