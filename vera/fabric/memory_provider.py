"""Portable memory projection contract and deterministic reference provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from vera.fabric.dataset_provider import (
    CancellationSignal,
    _cursor,
    _read_cursor,
)


MEMORY_SCHEMA = "vera.memory-projection/v1"
MEMORY_QUERY_SCHEMA = "vera.memory-query/v1"
MAX_MEMORY_PAGE = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MEMORY_ID = re.compile(r"^mem_[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^rec_[A-Za-z0-9._:-]{1,251}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"[a-z0-9]{2,}")
_URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]{1,31}$")


class MemoryAccessDenied(PermissionError):
    pass


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


def _json(value: Any, field_name: str, *, max_bytes: int = 65_536) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{field_name} must be finite JSON") from exc
    if json.loads(encoded) != value:
        raise ValueError(f"{field_name} must use JSON string object keys")
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field_name} exceeds size limit")
    return encoded


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value, "identity", max_bytes=1_048_576).encode()).hexdigest()


@dataclass(frozen=True)
class MemoryAccessContext:
    tenant_id: str
    principal_id: str
    session_id: str = ""
    purpose: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "principal_id", _identifier(
            self.principal_id, "principal_id"))
        if self.session_id:
            object.__setattr__(self, "session_id", _identifier(
                self.session_id, "session_id"))
        if len(str(self.purpose or "")) > 256:
            raise ValueError("purpose exceeds size limit")
        object.__setattr__(self, "purpose", str(self.purpose or "").strip())
        if self.request_id:
            object.__setattr__(self, "request_id", _identifier(
                self.request_id, "request_id"))


@dataclass(frozen=True, init=False)
class MemoryCitation:
    citation_id: str
    uri: str
    record_id: str
    revision_id: str
    label: str = ""
    locator_json: str = field(default="{}", repr=False)

    def __init__(self, *, citation_id: str, uri: str, record_id: str,
                 revision_id: str, label: str = "",
                 locator: Mapping[str, Any] | None = None):
        citation_id = _identifier(citation_id, "citation_id")
        uri = str(uri or "").strip()
        if len(uri) > 2048:
            raise ValueError("citation URI exceeds size limit")
        parsed = urlsplit(uri)
        if not _URI_SCHEME.fullmatch(parsed.scheme) or parsed.username or parsed.password:
            raise ValueError("invalid or credential-bearing citation URI")
        if not _RECORD_ID.fullmatch(str(record_id or "")):
            raise ValueError("invalid citation record_id")
        if not _REVISION_ID.fullmatch(str(revision_id or "")):
            raise ValueError("invalid citation revision_id")
        label = str(label or "").strip()
        if len(label) > 512:
            raise ValueError("citation label exceeds size limit")
        if locator is None:
            locator = {}
        if not isinstance(locator, Mapping):
            raise ValueError("citation locator must be an object")
        values = (citation_id, uri, record_id, revision_id, label,
                  _json(dict(locator), "citation locator", max_bytes=8192))
        for name, value in zip(("citation_id", "uri", "record_id", "revision_id",
                                "label", "locator_json"), values):
            object.__setattr__(self, name, value)

    @classmethod
    def create(cls, **values) -> "MemoryCitation":
        return cls(**values)

    @property
    def locator(self) -> dict:
        return json.loads(self.locator_json)

    def to_dict(self) -> dict:
        return {"citation_id": self.citation_id, "uri": self.uri,
                "record_id": self.record_id, "revision_id": self.revision_id,
                "label": self.label, "locator": self.locator}


@dataclass(frozen=True, init=False)
class MemoryProjection:
    memory_id: str
    tenant_id: str
    namespace: str
    record_id: str
    revision_id: str
    session_id: str
    record_type: str
    created_at: str
    updated_at: str
    importance: float
    tombstone: bool
    text: str
    citations: tuple[MemoryCitation, ...]
    tags: tuple[str, ...]
    policy_json: str = field(repr=False)
    metadata_json: str = field(repr=False)
    schema: str = MEMORY_SCHEMA

    def __init__(self, *, tenant_id: str, namespace: str, record_id: str,
                 revision_id: str, source_content_hash: str,
                 record_type: str, created_at: str,
                 text: str, citations: Sequence[MemoryCitation],
                 session_id: str = "", updated_at: str = "",
                 importance: float = 0.5, tombstone: bool = False,
                 tags: Sequence[str] = (), policy: Mapping[str, Any] | None = None,
                 metadata: Mapping[str, Any] | None = None):
        tenant_id = _identifier(tenant_id, "tenant_id")
        namespace = _identifier(namespace, "namespace")
        record_type = _identifier(record_type, "record_type")
        session_id = (_identifier(session_id, "session_id") if session_id else "")
        if not _RECORD_ID.fullmatch(str(record_id or "")):
            raise ValueError("invalid record_id")
        if not _REVISION_ID.fullmatch(str(revision_id or "")):
            raise ValueError("invalid revision_id")
        source_content_hash = str(source_content_hash or "").lower()
        if not _SHA256.fullmatch(source_content_hash):
            raise ValueError("invalid source_content_hash")
        created_at = _timestamp(created_at, "created_at")
        updated_at = _timestamp(updated_at or created_at, "updated_at")
        if datetime.fromisoformat(updated_at.replace("Z", "+00:00")) < \
                datetime.fromisoformat(created_at.replace("Z", "+00:00")):
            raise ValueError("updated_at cannot precede created_at")
        importance = float(importance)
        if not math.isfinite(importance) or not 0 <= importance <= 1:
            raise ValueError("importance must be finite and between 0 and 1")
        text = str(text or "")
        if len(text.encode("utf-8")) > 65_536:
            raise ValueError("memory text exceeds size limit")
        if bool(tombstone) != (text == ""):
            raise ValueError("active memory needs text; tombstone must not carry text")
        frozen_citations = tuple(citations)
        if not frozen_citations or len(frozen_citations) > 32 or not all(
                isinstance(item, MemoryCitation) for item in frozen_citations):
            raise ValueError("memory requires 1 to 32 citations")
        if len({item.citation_id for item in frozen_citations}) != len(frozen_citations):
            raise ValueError("citation IDs must be unique")
        if not any(item.record_id == record_id and item.revision_id == revision_id
                   for item in frozen_citations):
            raise ValueError("a citation must bind the authoritative revision")
        frozen_tags = tuple(sorted({_identifier(item, "tag") for item in tags}))
        if len(frozen_tags) > 64:
            raise ValueError("at most 64 tags are allowed")
        if policy is None:
            policy = {}
        if metadata is None:
            metadata = {}
        if not isinstance(policy, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("policy and metadata must be objects")
        memory_id = "mem_" + hashlib.sha256(
            f"{tenant_id}\0{namespace}\0{record_id}".encode()).hexdigest()
        values = {
            "memory_id": memory_id, "tenant_id": tenant_id,
            "namespace": namespace, "record_id": record_id,
            "revision_id": revision_id,
            "source_content_hash": source_content_hash,
            "projection_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
            "session_id": session_id,
            "record_type": record_type, "created_at": created_at,
            "updated_at": updated_at, "importance": importance,
            "tombstone": bool(tombstone), "text": text,
            "citations": frozen_citations, "tags": frozen_tags,
            "policy_json": _json(dict(policy), "policy"),
            "metadata_json": _json(dict(metadata), "metadata"),
            "schema": MEMORY_SCHEMA,
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @property
    def policy(self) -> dict:
        return json.loads(self.policy_json)

    @property
    def metadata(self) -> dict:
        return json.loads(self.metadata_json)

    def to_dict(self, *, include_text: bool = True) -> dict:
        return {"schema": self.schema, "memory_id": self.memory_id,
                "tenant_id": self.tenant_id, "namespace": self.namespace,
                "record_id": self.record_id, "revision_id": self.revision_id,
                "source_content_hash": self.source_content_hash,
                "projection_hash": self.projection_hash,
                "session_id": self.session_id, "record_type": self.record_type,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "importance": self.importance, "tombstone": self.tombstone,
                "text": self.text if include_text else "",
                "citations": [item.to_dict() for item in self.citations],
                "tags": list(self.tags), "policy": self.policy,
                "metadata": self.metadata}


@dataclass(frozen=True, init=False)
class MemoryQuery:
    tenant_id: str
    text: str
    namespace: str
    session_id: str
    record_type: str
    tags: tuple[str, ...]
    include_tombstones: bool
    include_text: bool
    limit: int
    cursor: str

    def __init__(self, *, tenant_id: str, text: str = "", namespace: str = "",
                 session_id: str = "", record_type: str = "",
                 tags: Sequence[str] = (), include_tombstones: bool = False,
                 include_text: bool = True, limit: int = 50, cursor: str = ""):
        object.__setattr__(self, "tenant_id", _identifier(tenant_id, "tenant_id"))
        for name, value in (("namespace", namespace), ("session_id", session_id),
                            ("record_type", record_type)):
            object.__setattr__(self, name, _identifier(value, name) if value else "")
        text = str(text or "").strip()
        if len(text.encode("utf-8")) > 4096:
            raise ValueError("memory query exceeds size limit")
        object.__setattr__(self, "text", text)
        frozen_tags = tuple(sorted({_identifier(item, "tag") for item in tags}))
        if len(frozen_tags) > 64:
            raise ValueError("at most 64 query tags are allowed")
        object.__setattr__(self, "tags", frozen_tags)
        object.__setattr__(self, "include_tombstones", bool(include_tombstones))
        object.__setattr__(self, "include_text", bool(include_text))
        limit = int(limit)
        if limit < 1 or limit > MAX_MEMORY_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_MEMORY_PAGE}")
        object.__setattr__(self, "limit", limit)
        cursor = str(cursor or "")
        if len(cursor) > 16_384:
            raise ValueError("cursor exceeds size limit")
        object.__setattr__(self, "cursor", cursor)

    @property
    def query_id(self) -> str:
        return "mqry_" + _hash({
            "schema": MEMORY_QUERY_SCHEMA, "tenant_id": self.tenant_id,
            "text": self.text, "namespace": self.namespace,
            "session_id": self.session_id, "record_type": self.record_type,
            "tags": list(self.tags), "include_tombstones": self.include_tombstones,
            "include_text": self.include_text,
        })


@dataclass(frozen=True, init=False)
class MemoryHit:
    score: float
    projection_json: str = field(repr=False)

    def __init__(self, score: float, projection: Mapping[str, Any]):
        object.__setattr__(self, "score", float(score))
        object.__setattr__(self, "projection_json", _json(
            dict(projection), "memory hit", max_bytes=131_072))

    @property
    def projection(self) -> dict:
        return json.loads(self.projection_json)

    def to_dict(self) -> dict:
        return {"score": self.score, "projection": self.projection}


@dataclass(frozen=True)
class MemoryPage:
    query_id: str
    hits: tuple[MemoryHit, ...]
    next_cursor: str
    provider: str
    generation: int

    def to_dict(self) -> dict:
        return {"query_id": self.query_id,
                "hits": [item.to_dict() for item in self.hits],
                "next_cursor": self.next_cursor, "provider": self.provider,
                "generation": self.generation}


class MemoryProvider(Protocol):
    name: str
    def apply(self, projection: MemoryProjection,
              access: MemoryAccessContext) -> MemoryProjection: ...
    def get(self, memory_id: str, access: MemoryAccessContext,
            *, include_text: bool = True) -> dict: ...
    def search(self, query: MemoryQuery, access: MemoryAccessContext, *,
               cancellation: CancellationSignal | None = None) -> MemoryPage: ...


MemoryAuthorizer = Callable[[str, MemoryAccessContext, Mapping[str, Any]], bool]


class FrozenMemoryProvider:
    """Bounded offline reference. It supplies contract behavior, not ranking quality."""

    name = "frozen-memory"

    def __init__(self, *, authorizer: MemoryAuthorizer | None = None,
                 max_records: int = 10_000):
        self._authorize = authorizer or (lambda operation, access, context: False)
        self._max_records = max(1, int(max_records))
        self._records: dict[str, MemoryProjection] = {}
        self._generation = 0

    def _allowed(self, operation: str, access: MemoryAccessContext,
                 context: Mapping[str, Any]) -> None:
        safe = json.loads(_json(dict(context), "authorization context", max_bytes=8192))
        if not bool(self._authorize(operation, access, safe)):
            raise MemoryAccessDenied(f"memory {operation} denied")

    @staticmethod
    def _context(item: MemoryProjection) -> dict:
        return {"tenant_id": item.tenant_id, "namespace": item.namespace,
                "session_id": item.session_id, "record_type": item.record_type,
                "memory_id": item.memory_id, "record_id": item.record_id,
                "revision_id": item.revision_id, "tombstone": item.tombstone,
                "source_content_hash": item.source_content_hash,
                "policy": item.policy}

    def apply(self, projection: MemoryProjection,
              access: MemoryAccessContext) -> MemoryProjection:
        if not isinstance(projection, MemoryProjection):
            raise TypeError("projection must be MemoryProjection")
        if access.tenant_id != projection.tenant_id:
            raise MemoryAccessDenied("cross-tenant memory apply denied")
        self._allowed("apply", access, self._context(projection))
        current = self._records.get(projection.memory_id)
        if current == projection:
            return current
        if current and (projection.created_at, projection.session_id,
                        projection.record_type) != (
                            current.created_at, current.session_id,
                            current.record_type):
            raise ValueError("memory projection identity context is immutable")
        if current and datetime.fromisoformat(
                projection.updated_at.replace("Z", "+00:00")) <= datetime.fromisoformat(
                    current.updated_at.replace("Z", "+00:00")):
            raise ValueError("memory projection must advance updated_at")
        if not current and len(self._records) >= self._max_records:
            raise ValueError("memory provider record limit reached")
        self._records[projection.memory_id] = projection
        self._generation += 1
        return projection

    def get(self, memory_id: str, access: MemoryAccessContext,
            *, include_text: bool = True) -> dict:
        if not _MEMORY_ID.fullmatch(str(memory_id or "")):
            raise ValueError("invalid memory_id")
        item = self._records.get(memory_id)
        if not item or item.tenant_id != access.tenant_id:
            raise KeyError("memory not found")
        self._allowed("read", access, self._context(item))
        return item.to_dict(include_text=include_text)

    @staticmethod
    def _score(text: str, item: MemoryProjection) -> float:
        wanted = set(_TOKEN.findall(text.casefold()))
        if not wanted:
            return 0.0
        present = set(_TOKEN.findall(item.text.casefold()))
        return len(wanted & present) / len(wanted)

    def search(self, query: MemoryQuery, access: MemoryAccessContext, *,
               cancellation: CancellationSignal | None = None) -> MemoryPage:
        if query.tenant_id != access.tenant_id:
            raise MemoryAccessDenied("cross-tenant memory search denied")
        signal = cancellation or CancellationSignal()
        signal.checkpoint()
        query_context = {"tenant_id": query.tenant_id,
                         "namespace": query.namespace,
                         "session_id": query.session_id,
                         "record_type": query.record_type,
                         "include_tombstones": query.include_tombstones}
        self._allowed("search", access, query_context)
        identity = f"{query.query_id}:{self._generation}"
        offset = _read_cursor(query.cursor, kind="memory-search", identity=identity)
        wanted_tags = set(query.tags)
        candidates = []
        for item in self._records.values():
            signal.checkpoint()
            if item.tenant_id != query.tenant_id:
                continue
            if query.namespace and item.namespace != query.namespace:
                continue
            if query.session_id and item.session_id != query.session_id:
                continue
            if query.record_type and item.record_type != query.record_type:
                continue
            if wanted_tags and not wanted_tags.issubset(item.tags):
                continue
            if item.tombstone and not query.include_tombstones:
                continue
            try:
                self._allowed("read", access, self._context(item))
            except MemoryAccessDenied:
                continue
            score = self._score(query.text, item)
            if query.text and score == 0:
                continue
            candidates.append((score, item))
        candidates.sort(key=lambda value: (
            -value[0], -value[1].importance,
            -datetime.fromisoformat(value[1].created_at.replace("Z", "+00:00")).timestamp(),
            value[1].memory_id))
        selected = candidates[offset:offset + query.limit]
        hits = tuple(MemoryHit(score, item.to_dict(include_text=query.include_text))
                     for score, item in selected)
        next_offset = offset + len(hits)
        next_cursor = (_cursor({"kind": "memory-search", "identity": identity,
                                "offset": next_offset})
                       if next_offset < len(candidates) else "")
        return MemoryPage(query.query_id, hits, next_cursor, self.name,
                          self._generation)
