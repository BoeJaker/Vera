"""Portable dataset/query contracts and a deterministic reference provider."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
from threading import Event
from typing import Any, Mapping, Protocol, Sequence


SCHEMA_VERSION = "vera.dataset-snapshot/v1"
QUERY_VERSION = "vera.dataset-query/v1"
MAX_PAGE_SIZE = 1000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{64}$")


class QueryCancelled(RuntimeError):
    pass


class CancellationSignal:
    """Process-local cooperative signal; adapters map native cancellation to it."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise QueryCancelled("dataset operation cancelled")


def _identifier(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


def _timestamp(value: str, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_copy(value: Any, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be canonical JSON") from exc


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _limit(value: int) -> int:
    value = int(value)
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _cursor(payload: Mapping[str, Any]) -> str:
    body = _json_copy(dict(payload), "cursor")
    envelope = {"body": body, "checksum": _hash(body)}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(value: str, *, kind: str, identity: str) -> int:
    if not value:
        return 0
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        envelope = json.loads(raw)
        body = envelope["body"]
        if envelope["checksum"] != _hash(body):
            raise ValueError
        if body != {"kind": kind, "identity": identity,
                    "offset": int(body["offset"])}:
            raise ValueError
        offset = int(body["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or mismatched cursor") from exc


@dataclass(frozen=True, init=False)
class DatasetSnapshot:
    dataset_id: str
    snapshot_id: str
    created_at: str
    record_count: int
    _schema_json: str = field(repr=False)
    _provenance_json: str = field(repr=False)
    schema_version: str = SCHEMA_VERSION

    def __init__(self, *, dataset_id: str, snapshot_id: str, created_at: str,
                 record_count: int, schema: Mapping[str, Any],
                 provenance: Mapping[str, Any], schema_version: str = SCHEMA_VERSION):
        object.__setattr__(self, "dataset_id", _identifier(dataset_id, "dataset_id"))
        if not _SNAPSHOT_ID.fullmatch(str(snapshot_id or "")):
            raise ValueError("invalid snapshot_id")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "created_at", _timestamp(created_at, "created_at"))
        if int(record_count) < 0:
            raise ValueError("record_count must not be negative")
        object.__setattr__(self, "record_count", int(record_count))
        if schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported dataset snapshot schema_version")
        object.__setattr__(self, "schema_version", schema_version)
        if not isinstance(schema, Mapping) or not isinstance(provenance, Mapping):
            raise ValueError("schema and provenance must be JSON objects")
        object.__setattr__(self, "_schema_json", json.dumps(
            _json_copy(dict(schema), "schema"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False))
        object.__setattr__(self, "_provenance_json", json.dumps(
            _json_copy(dict(provenance), "provenance"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False))

    @property
    def schema(self) -> dict:
        return json.loads(self._schema_json)

    @property
    def provenance(self) -> dict:
        return json.loads(self._provenance_json)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "dataset_id": self.dataset_id,
                "snapshot_id": self.snapshot_id, "created_at": self.created_at,
                "record_count": self.record_count, "schema": self.schema,
                "provenance": self.provenance}

    @classmethod
    def create(cls, *, dataset_id: str, created_at: str,
               records: Sequence[Mapping[str, Any]], schema: Mapping[str, Any],
               provenance: Mapping[str, Any]) -> tuple["DatasetSnapshot", tuple[dict, ...]]:
        dataset_id = _identifier(dataset_id, "dataset_id")
        created_at = _timestamp(created_at, "created_at")
        if not isinstance(schema, Mapping) or not isinstance(provenance, Mapping):
            raise ValueError("schema and provenance must be JSON objects")
        frozen_records = tuple(_json_copy(dict(row), "records") for row in records)
        frozen_schema = _json_copy(dict(schema), "schema")
        frozen_provenance = _json_copy(dict(provenance), "provenance")
        identity = {
            "schema_version": SCHEMA_VERSION, "dataset_id": dataset_id,
            "created_at": created_at, "schema": frozen_schema,
            "provenance": frozen_provenance, "records": frozen_records,
        }
        snapshot = cls(
            dataset_id=dataset_id, snapshot_id="snap_" + _hash(identity),
            created_at=created_at, record_count=len(frozen_records),
            schema=frozen_schema, provenance=frozen_provenance,
        )
        return snapshot, frozen_records

@dataclass(frozen=True)
class DatasetPage:
    snapshot: DatasetSnapshot
    records: tuple[dict, ...]
    next_cursor: str
    provider: str

    def to_dict(self) -> dict:
        return {"snapshot": self.snapshot.to_dict(), "records": list(self.records),
                "next_cursor": self.next_cursor, "provider": self.provider}


@dataclass(frozen=True, init=False)
class QueryRequest:
    dataset_id: str
    snapshot_id: str
    text: str = ""
    _filters_json: str = field(default="{}", repr=False)
    limit: int = 50
    cursor: str = ""
    include_data: bool = False

    def __init__(self, *, dataset_id: str, snapshot_id: str, text: str = "",
                 filters: Mapping[str, Any] | None = None, limit: int = 50,
                 cursor: str = "", include_data: bool = False):
        object.__setattr__(self, "dataset_id", _identifier(dataset_id, "dataset_id"))
        if not _SNAPSHOT_ID.fullmatch(str(snapshot_id or "")):
            raise ValueError("invalid snapshot_id")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        if filters is None:
            filters = {}
        if not isinstance(filters, Mapping):
            raise ValueError("filters must be a JSON object")
        frozen_filters = _json_copy(dict(filters), "filters")
        object.__setattr__(self, "_filters_json", json.dumps(
            frozen_filters, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False))
        object.__setattr__(self, "text", str(text or "").strip())
        object.__setattr__(self, "limit", _limit(limit))
        object.__setattr__(self, "cursor", str(cursor or ""))
        object.__setattr__(self, "include_data", bool(include_data))

    @property
    def filters(self) -> dict:
        return json.loads(self._filters_json)

    @property
    def query_id(self) -> str:
        return "qry_" + _hash({
            "schema_version": QUERY_VERSION, "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id, "text": self.text,
            "filters": self.filters, "include_data": bool(self.include_data),
        })


@dataclass(frozen=True)
class QueryPage:
    query_id: str
    snapshot_id: str
    matches: tuple[dict, ...]
    next_cursor: str
    provider: str
    provenance: dict

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetProvider(Protocol):
    name: str
    def stat(self, dataset_id: str, snapshot_id: str = "") -> DatasetSnapshot: ...
    def scan(self, snapshot_id: str, *, limit: int = 50, cursor: str = "",
             cancellation: CancellationSignal | None = None) -> DatasetPage: ...


class QueryProvider(Protocol):
    name: str
    def query(self, request: QueryRequest, *,
              cancellation: CancellationSignal | None = None) -> QueryPage: ...


class FrozenDatasetProvider:
    """Offline reference adapter; deliberately supports only equality filters."""

    name = "frozen"

    def __init__(self) -> None:
        self._snapshots: dict[str, tuple[DatasetSnapshot, tuple[dict, ...]]] = {}
        self._latest: dict[str, str] = {}

    def register(self, *, dataset_id: str, created_at: str,
                 records: Sequence[Mapping[str, Any]], schema: Mapping[str, Any],
                 provenance: Mapping[str, Any]) -> DatasetSnapshot:
        snapshot, frozen = DatasetSnapshot.create(
            dataset_id=dataset_id, created_at=created_at, records=records,
            schema=schema, provenance=provenance)
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing and existing != (snapshot, frozen):
            raise ValueError("snapshot identity collision")
        self._snapshots[snapshot.snapshot_id] = (snapshot, frozen)
        current_id = self._latest.get(snapshot.dataset_id)
        current = self._snapshots.get(current_id) if current_id else None
        if not current or current[0].created_at <= snapshot.created_at:
            self._latest[snapshot.dataset_id] = snapshot.snapshot_id
        return snapshot

    def stat(self, dataset_id: str, snapshot_id: str = "") -> DatasetSnapshot:
        dataset_id = _identifier(dataset_id, "dataset_id")
        wanted = snapshot_id or self._latest.get(dataset_id, "")
        found = self._snapshots.get(wanted)
        if not found or found[0].dataset_id != dataset_id:
            raise KeyError("dataset snapshot not found")
        return found[0]

    def scan(self, snapshot_id: str, *, limit: int = 50, cursor: str = "",
             cancellation: CancellationSignal | None = None) -> DatasetPage:
        found = self._snapshots.get(snapshot_id)
        if not found:
            raise KeyError("dataset snapshot not found")
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        limit = _limit(limit)
        offset = _read_cursor(cursor, kind="scan", identity=snapshot_id)
        snapshot, records = found
        page = tuple(_json_copy(row, "record") for row in records[offset:offset + limit])
        signal.checkpoint()
        next_offset = offset + len(page)
        next_cursor = (_cursor({"kind": "scan", "identity": snapshot_id,
                                "offset": next_offset})
                       if next_offset < len(records) else "")
        return DatasetPage(snapshot, page, next_cursor, self.name)

    def query(self, request: QueryRequest, *,
              cancellation: CancellationSignal | None = None) -> QueryPage:
        snapshot = self.stat(request.dataset_id, request.snapshot_id)
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        records = self._snapshots[snapshot.snapshot_id][1]
        needle = request.text.casefold()
        matches = []
        for index, row in enumerate(records):
            signal.checkpoint()
            if any(row.get(key) != value for key, value in request.filters.items()):
                continue
            searchable = json.dumps(row, sort_keys=True, ensure_ascii=False).casefold()
            if needle and needle not in searchable:
                continue
            item = {"record_index": index, "score": 1.0 if needle else 0.0}
            if request.include_data:
                item["data"] = _json_copy(row, "record")
            matches.append(item)
        offset = _read_cursor(
            request.cursor, kind="query", identity=request.query_id)
        page = tuple(matches[offset:offset + request.limit])
        next_offset = offset + len(page)
        next_cursor = (_cursor({"kind": "query", "identity": request.query_id,
                                "offset": next_offset})
                       if next_offset < len(matches) else "")
        return QueryPage(
            query_id=request.query_id, snapshot_id=snapshot.snapshot_id,
            matches=page, next_cursor=next_cursor, provider=self.name,
            provenance=_json_copy(snapshot.provenance, "provenance"))
