"""Portable dataset boundary for non-JEPA Worldview and Godseye data.

The adapter accepts records that Godseye's pure parsers have already produced.
It performs no source fetch, database read, UI inspection, or JEPA inference.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from vera.fabric.dataset_provider import DatasetSnapshot


SCHEMA_VERSION = "vera.godseye-portable-dataset/v1"
MAX_RECORDS = 5_000
MAX_RECORD_BYTES = 1_000_000
MAX_GEOMETRY_COORDINATES = 20_000
KINDS = {"cctv", "imagery", "buildings"}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("records must be canonical JSON") from exc


def _coordinate(value: Any, *, latitude: bool) -> float:
    if isinstance(value, bool):
        raise ValueError("coordinates must be numbers")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinates must be numbers") from exc
    limit = 90.0 if latitude else 180.0
    if not -limit <= number <= limit:
        raise ValueError("coordinate out of range")
    return number


def _normalise(kind: str, row: Mapping[str, Any], *, source_revision: str) -> dict:
    if not isinstance(row, Mapping):
        raise ValueError("records must be JSON objects")
    record_id = str(row.get("id") or "").strip()
    if not record_id or len(record_id) > 256:
        raise ValueError("every record needs a bounded id")
    value = dict(row)
    if kind in {"cctv", "imagery"}:
        value["lat"] = _coordinate(value.get("lat"), latitude=True)
        value["lng"] = _coordinate(value.get("lng"), latitude=False)
    else:
        coords = value.get("coords")
        if not isinstance(coords, list) or len(coords) < 6 or len(coords) % 2:
            raise ValueError("building coords must contain longitude/latitude pairs")
        if len(coords) > MAX_GEOMETRY_COORDINATES:
            raise ValueError("building geometry exceeds the coordinate limit")
        value["coords"] = [
            _coordinate(item, latitude=bool(index % 2))
            for index, item in enumerate(coords)
        ]
    value["record_id"] = record_id
    value["record_revision"] = "rev_" + hashlib.sha256(_canonical({
        "kind": kind, "source_revision": source_revision, "record": value,
    })).hexdigest()
    if len(_canonical(value)) > MAX_RECORD_BYTES:
        raise ValueError("record exceeds the encoded size limit")
    return json.loads(_canonical(value))


@dataclass(frozen=True)
class PortableGeospatialDataset:
    """Immutable snapshot plus content authority for its record artifact."""

    snapshot: DatasetSnapshot
    records: tuple[dict, ...]
    artifact_id: str
    kind: str
    schema_version: str = SCHEMA_VERSION

    def manifest(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "snapshot": self.snapshot.to_dict(),
            "artifact_id": self.artifact_id,
        }


def make_portable_dataset(*, kind: str, records: Sequence[Mapping[str, Any]],
                          source_revision: str, created_at: str,
                          source: str = "godseye") -> PortableGeospatialDataset:
    """Bind normalized geospatial records to exact source and record revisions."""
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError("unsupported geospatial dataset kind")
    source = str(source or "").strip().lower()
    if source not in {"godseye", "worldview-non-jepa"}:
        raise ValueError("source must identify Godseye or non-JEPA Worldview")
    source_revision = str(source_revision or "").strip()
    if not source_revision or len(source_revision) > 256:
        raise ValueError("source_revision must be present and bounded")
    if isinstance(records, (str, bytes)) or len(records) > MAX_RECORDS:
        raise ValueError(f"records must contain at most {MAX_RECORDS} entries")

    normalised = [_normalise(kind, row, source_revision=source_revision)
                  for row in records]
    ids = [row["record_id"] for row in normalised]
    if len(ids) != len(set(ids)):
        raise ValueError("record ids must be unique")
    ordered = tuple(sorted(normalised, key=lambda row: row["record_id"]))
    provenance = {
        "system": source,
        "lineage": "non-jepa-worldview-godseye",
        "source_revision": source_revision,
        "record_revision_field": "record_revision",
        "jepa_authority": False,
    }
    schema = {
        "type": "object",
        "required": ["record_id", "record_revision"],
        "additionalProperties": True,
        "geospatial_kind": kind,
    }
    snapshot, frozen = DatasetSnapshot.create(
        dataset_id=f"{source}.{kind}", created_at=created_at, records=ordered,
        schema=schema, provenance=provenance,
    )
    artifact_id = "artifact_sha256:" + hashlib.sha256(_canonical({
        "snapshot_id": snapshot.snapshot_id, "records": frozen,
    })).hexdigest()
    return PortableGeospatialDataset(snapshot, frozen, artifact_id, kind)
