"""Project portable Worldview/Godseye snapshots into JEPA input evidence.

This is an offline identity adapter, not a model or retrieval adapter.  It
verifies the complete portable artifact and exposes only revision and content
hash evidence that a later JEPA comparison may cite.  It never grants JEPA
authority, reads a provider, or changes a consumer route.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from ..fabric.dataset_provider import DatasetSnapshot
from ..godseye.portable_dataset import KINDS, PortableGeospatialDataset


SCHEMA = "vera.worldview-lineage-dataset-projection/v1"
TARGET_CONTRACT = "vera.dataset-snapshot/v1"
_REVISION = re.compile(r"^rev_[0-9a-f]{64}$")
_ARTIFACT = re.compile(r"^artifact_sha256:[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SOURCE_LINEAGES = {
    "godseye": "godseye.integrated",
    "worldview-non-jepa": "worldview.non-jepa",
}


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("projection inputs must be canonical JSON") from exc


def _sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LineageDatasetProjection:
    projection_id: str
    source_lineage: str
    source_system: str
    source_revision: str
    source_artifact_id: str
    dataset_id: str
    snapshot_id: str
    geospatial_kind: str
    record_count: int
    record_revisions: tuple[tuple[str, str], ...]
    record_content_hashes: tuple[tuple[str, str], ...]
    target_contract: str = TARGET_CONTRACT
    intended_consumer: str = "worldview.jepa"
    authority: str = "candidate_evidence_only"
    schema: str = SCHEMA

    def citation_revisions(self) -> dict[str, str]:
        """Return the exact mapping accepted by the JEPA result projector."""
        return dict(self.record_revisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "projection_id": self.projection_id,
            "source_lineage": self.source_lineage,
            "source_system": self.source_system,
            "source_revision": self.source_revision,
            "source_artifact_id": self.source_artifact_id,
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "geospatial_kind": self.geospatial_kind,
            "record_count": self.record_count,
            "record_revisions": [
                {"record_id": record_id, "revision_id": revision_id}
                for record_id, revision_id in self.record_revisions
            ],
            "record_content_hashes": [
                {"record_id": record_id, "content_hash": content_hash}
                for record_id, content_hash in self.record_content_hashes
            ],
            "target_contract": self.target_contract,
            "intended_consumer": self.intended_consumer,
            "authority": self.authority,
            "eligible_for_influence": False,
            "executes": False,
            "runtime_changed": False,
        }


def _verify_snapshot(dataset: PortableGeospatialDataset) -> None:
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id=dataset.snapshot.dataset_id,
        created_at=dataset.snapshot.created_at,
        records=dataset.records,
        schema=dataset.snapshot.schema,
        provenance=dataset.snapshot.provenance,
    )
    if snapshot != dataset.snapshot or frozen != dataset.records:
        raise ValueError("portable dataset snapshot identity does not verify")
    expected_artifact = "artifact_sha256:" + hashlib.sha256(_canonical({
        "snapshot_id": snapshot.snapshot_id,
        "records": frozen,
    }).encode()).hexdigest()
    if not _ARTIFACT.fullmatch(dataset.artifact_id) or dataset.artifact_id != expected_artifact:
        raise ValueError("portable dataset artifact identity does not verify")


def project_lineage_dataset(
        dataset: PortableGeospatialDataset) -> LineageDatasetProjection:
    """Verify one portable snapshot and expose payload-free JEPA citations."""
    if not isinstance(dataset, PortableGeospatialDataset):
        raise TypeError("dataset must be PortableGeospatialDataset")
    _verify_snapshot(dataset)
    provenance = dataset.snapshot.provenance
    source = str(provenance.get("system") or "")
    if source not in _SOURCE_LINEAGES:
        raise ValueError("snapshot source is not a supported Worldview lineage")
    if provenance.get("lineage") != "non-jepa-worldview-godseye":
        raise ValueError("snapshot lineage is not the portable geospatial lineage")
    if provenance.get("jepa_authority") is not False:
        raise ValueError("portable source must not claim JEPA authority")
    if provenance.get("record_revision_field") != "record_revision":
        raise ValueError("snapshot does not declare the record revision field")
    source_revision = str(provenance.get("source_revision") or "").strip()
    if not source_revision or len(source_revision) > 256:
        raise ValueError("snapshot source revision must be present and bounded")
    if dataset.snapshot.dataset_id != f"{source}.{dataset.kind}":
        raise ValueError("snapshot dataset identity does not match its lineage and kind")
    if (dataset.kind not in KINDS or
            dataset.snapshot.schema.get("geospatial_kind") != dataset.kind):
        raise ValueError("snapshot kind does not match its schema")
    if not dataset.records:
        raise ValueError("projection requires at least one portable record")

    revisions: list[tuple[str, str]] = []
    hashes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for record in dataset.records:
        record_id = str(record.get("record_id") or "")
        revision = str(record.get("record_revision") or "")
        if not _RECORD_ID.fullmatch(record_id) or record_id in seen:
            raise ValueError("projected record identities must be present and unique")
        if not _REVISION.fullmatch(revision):
            raise ValueError("projected records require exact portable revisions")
        revision_input = dict(record)
        revision_input.pop("record_revision", None)
        expected_revision = "rev_" + hashlib.sha256(_canonical({
            "kind": dataset.kind,
            "source_revision": source_revision,
            "record": revision_input,
        }).encode()).hexdigest()
        if revision != expected_revision:
            raise ValueError("portable record revision does not verify")
        seen.add(record_id)
        revisions.append((record_id, revision))
        hashes.append((record_id, _sha(record)))
    revisions.sort()
    hashes.sort()
    identity = {
        "schema": SCHEMA,
        "source_lineage": _SOURCE_LINEAGES[source],
        "source_system": source,
        "source_revision": source_revision,
        "source_artifact_id": dataset.artifact_id,
        "dataset_id": dataset.snapshot.dataset_id,
        "snapshot_id": dataset.snapshot.snapshot_id,
        "geospatial_kind": dataset.kind,
        "record_revisions": revisions,
        "record_content_hashes": hashes,
        "target_contract": TARGET_CONTRACT,
        "intended_consumer": "worldview.jepa",
        "authority": "candidate_evidence_only",
    }
    return LineageDatasetProjection(
        projection_id="wvlproj_" + hashlib.sha256(
            _canonical(identity).encode()).hexdigest(),
        source_lineage=_SOURCE_LINEAGES[source],
        source_system=source,
        source_revision=source_revision,
        source_artifact_id=dataset.artifact_id,
        dataset_id=dataset.snapshot.dataset_id,
        snapshot_id=dataset.snapshot.snapshot_id,
        geospatial_kind=dataset.kind,
        record_count=len(revisions),
        record_revisions=tuple(revisions),
        record_content_hashes=tuple(hashes),
    )
