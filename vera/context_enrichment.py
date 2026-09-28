"""Payload-free, append-only evidence for reciprocal context enrichment."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from vera.context_provider import ContextCitation, ContextItem

MAX_RECORDS = 10_000
MAX_PARENTS = 64
MAX_AUTHORITIES = 128
MAX_CITATIONS = 128
MAX_DEPTH = 16
MAX_ATTRIBUTES = 32
MAX_ATTRIBUTE_BYTES = 16_384
MAX_VALIDITY_MS = 365 * 24 * 60 * 60 * 1000


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be canonical non-empty text")
    return value


def _timestamp(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _id(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()[:24]


def _citations(values: Sequence[ContextCitation]) -> tuple[ContextCitation, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError("citations must be a sequence") from exc
    if not result or len(result) > MAX_CITATIONS or not all(
            isinstance(value, ContextCitation) for value in result):
        raise ValueError("one to 128 valid citations are required")
    return tuple(sorted(set(result), key=lambda x: (x.source_id, x.locator)))


@dataclass(frozen=True, slots=True, order=True)
class EnrichmentAuthority:
    kind: str
    identity: str
    revision: str
    locator: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"artifact", "context", "dataset", "receipt", "record"}:
            raise ValueError("unsupported enrichment authority kind")
        _text(self.identity, "authority identity")
        _text(self.revision, "authority revision")
        if not isinstance(self.locator, str) or self.locator != self.locator.strip():
            raise ValueError("authority locator must be canonical")


def context_authority(item: ContextItem) -> EnrichmentAuthority:
    if not isinstance(item, ContextItem):
        raise ValueError("context authority requires ContextItem")
    return EnrichmentAuthority("context", f"{item.provider}:{item.item_id}",
                               item.revision, item.citations[0].locator)


@dataclass(frozen=True, slots=True)
class ContextEnrichmentEvidence:
    producer: str
    producer_revision: str
    kind: str
    observed_at_ms: int
    valid_until_ms: int
    authorities: tuple[EnrichmentAuthority, ...]
    citations: tuple[ContextCitation, ...]
    parent_ids: tuple[str, ...] = ()
    score: float | None = None
    attributes: tuple[tuple[str, str | int | float | bool], ...] = ()
    evidence_id: str = ""

    def __post_init__(self) -> None:
        _text(self.producer, "producer")
        _text(self.producer_revision, "producer revision")
        _text(self.kind, "evidence kind")
        observed = _timestamp(self.observed_at_ms, "observed_at_ms")
        valid_until = _timestamp(self.valid_until_ms, "valid_until_ms")
        if valid_until < observed or valid_until - observed > MAX_VALIDITY_MS:
            raise ValueError("evidence validity is invalid or unbounded")
        try:
            authorities = tuple(self.authorities)
            parents = tuple(self.parent_ids)
            attributes = tuple(self.attributes)
        except TypeError as exc:
            raise ValueError("evidence collections must be sequences") from exc
        if not authorities or len(authorities) > MAX_AUTHORITIES or not all(
                isinstance(value, EnrichmentAuthority) for value in authorities):
            raise ValueError("one to 128 authorities are required")
        authorities = tuple(sorted(set(authorities)))
        citations = _citations(self.citations)
        if len(parents) > MAX_PARENTS or any(not isinstance(x, str) or not x.strip()
                                              for x in parents):
            raise ValueError("parent evidence identities are invalid")
        parents = tuple(sorted(set(parents)))
        if self.score is not None and (isinstance(self.score, bool) or
                not isinstance(self.score, (int, float)) or
                not math.isfinite(self.score) or not 0 <= self.score <= 1):
            raise ValueError("evidence score must be between zero and one")
        if len(attributes) > MAX_ATTRIBUTES:
            raise ValueError("too many enrichment attributes")
        normal: list[tuple[str, str | int | float | bool]] = []
        for pair in attributes:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("attributes must be key/value pairs")
            key, value = pair
            _text(key, "attribute key")
            if any(token in key.lower() for token in ("text", "content", "prompt", "vector", "embedding", "secret")):
                raise ValueError("payload-bearing enrichment attributes are forbidden")
            if not isinstance(value, (str, int, float, bool)) or (
                    isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("attribute values must be bounded scalars")
            if isinstance(value, str) and (len(value) > 512 or value != value.strip()):
                raise ValueError("attribute text must be short and canonical")
            normal.append((key, value))
        normal.sort(key=lambda x: x[0])
        if len({x[0] for x in normal}) != len(normal):
            raise ValueError("attribute keys must be unique")
        if len(json.dumps(normal, ensure_ascii=True).encode("utf-8")) > MAX_ATTRIBUTE_BYTES:
            raise ValueError("enrichment attributes exceed byte budget")
        object.__setattr__(self, "authorities", authorities)
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "parent_ids", parents)
        object.__setattr__(self, "attributes", tuple(normal))
        payload = self.to_dict(include_id=False)
        calculated = _id("cee_", payload)
        if self.evidence_id and self.evidence_id != calculated:
            raise ValueError("evidence identity does not match content")
        object.__setattr__(self, "evidence_id", calculated)

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "producer": self.producer, "producer_revision": self.producer_revision,
            "kind": self.kind, "observed_at_ms": self.observed_at_ms,
            "valid_until_ms": self.valid_until_ms,
            "authorities": [{"kind": x.kind, "identity": x.identity,
                             "revision": x.revision, "locator": x.locator}
                            for x in self.authorities],
            "citations": [{"source_id": x.source_id, "locator": x.locator}
                          for x in self.citations],
            "parent_ids": list(self.parent_ids), "score": self.score,
            "attributes": [list(x) for x in self.attributes],
        }
        if include_id:
            result["evidence_id"] = self.evidence_id
        return result


@dataclass(frozen=True, slots=True)
class ContextEnrichmentTombstone:
    producer: str
    target_evidence_id: str
    reason: str
    observed_at_ms: int
    citations: tuple[ContextCitation, ...]
    tombstone_id: str = ""

    def __post_init__(self) -> None:
        _text(self.producer, "tombstone producer")
        _text(self.target_evidence_id, "target evidence")
        if self.reason not in {"invalidated", "source_deleted", "superseded"}:
            raise ValueError("unsupported tombstone reason")
        _timestamp(self.observed_at_ms, "observed_at_ms")
        citations = _citations(self.citations)
        object.__setattr__(self, "citations", citations)
        payload = {"producer": self.producer, "target_evidence_id": self.target_evidence_id,
                   "reason": self.reason, "observed_at_ms": self.observed_at_ms,
                   "citations": [(x.source_id, x.locator) for x in citations]}
        calculated = _id("cet_", payload)
        if self.tombstone_id and self.tombstone_id != calculated:
            raise ValueError("tombstone identity does not match content")
        object.__setattr__(self, "tombstone_id", calculated)


@dataclass(frozen=True, slots=True)
class ContextEnrichmentLedger:
    evidence: tuple[ContextEnrichmentEvidence, ...] = ()
    tombstones: tuple[ContextEnrichmentTombstone, ...] = ()

    def __post_init__(self) -> None:
        try:
            evidence = tuple(self.evidence)
            tombstones = tuple(self.tombstones)
        except TypeError as exc:
            raise ValueError("ledger records must be sequences") from exc
        if len(evidence) + len(tombstones) > MAX_RECORDS:
            raise ValueError("enrichment ledger exceeds record budget")
        if not all(isinstance(x, ContextEnrichmentEvidence) for x in evidence) or not all(
                isinstance(x, ContextEnrichmentTombstone) for x in tombstones):
            raise ValueError("ledger contains invalid records")
        by_id = {x.evidence_id: x for x in evidence}
        if len(by_id) != len(evidence):
            raise ValueError("ledger contains duplicate evidence")
        ancestry: dict[str, tuple[set[str], int]] = {}
        remaining = set(by_id)
        while remaining:
            progressed = False
            for evidence_id in tuple(remaining):
                item = by_id[evidence_id]
                missing = [x for x in item.parent_ids if x not in by_id]
                if missing:
                    raise ValueError("parent evidence is missing")
                if not all(x in ancestry for x in item.parent_ids):
                    continue
                ancestor_producers: set[str] = set()
                depth = 1
                parent_authorities: set[EnrichmentAuthority] = set()
                parent_citations: set[ContextCitation] = set()
                for parent_id in item.parent_ids:
                    parent = by_id[parent_id]
                    producers, parent_depth = ancestry[parent_id]
                    ancestor_producers.update(producers)
                    ancestor_producers.add(parent.producer)
                    depth = max(depth, parent_depth + 1)
                    parent_authorities.update(parent.authorities)
                    parent_citations.update(parent.citations)
                    if item.observed_at_ms < parent.observed_at_ms:
                        raise ValueError("derived evidence predates its parent")
                    if item.valid_until_ms > parent.valid_until_ms:
                        raise ValueError("derived evidence outlives its parent")
                if item.producer in ancestor_producers:
                    raise ValueError("producer may not consume its own ancestry")
                if not parent_authorities.issubset(set(item.authorities)):
                    raise ValueError("derived evidence stripped authority")
                if not parent_citations.issubset(set(item.citations)):
                    raise ValueError("derived evidence stripped citation")
                if depth > MAX_DEPTH:
                    raise ValueError("enrichment lineage exceeds depth budget")
                ancestry[evidence_id] = (ancestor_producers, depth)
                remaining.remove(evidence_id)
                progressed = True
            if not progressed:
                raise ValueError("enrichment lineage contains a cycle")
        tombstoned: set[str] = set()
        for record in tombstones:
            target = by_id.get(record.target_evidence_id)
            if target is None:
                raise ValueError("tombstone target is missing")
            if record.target_evidence_id in tombstoned:
                raise ValueError("evidence may be tombstoned only once")
            if record.producer != target.producer:
                raise ValueError("only the evidence producer may tombstone it")
            if record.observed_at_ms < target.observed_at_ms:
                raise ValueError("tombstone predates evidence")
            if not set(target.citations).issubset(set(record.citations)):
                raise ValueError("tombstone stripped citation")
            tombstoned.add(record.target_evidence_id)
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda x: x.evidence_id)))
        object.__setattr__(self, "tombstones", tuple(sorted(tombstones, key=lambda x: x.tombstone_id)))

    def append(self, *records: ContextEnrichmentEvidence | ContextEnrichmentTombstone
               ) -> "ContextEnrichmentLedger":
        evidence = {x.evidence_id: x for x in self.evidence}
        tombstones = {x.tombstone_id: x for x in self.tombstones}
        for record in records:
            if isinstance(record, ContextEnrichmentEvidence):
                evidence.setdefault(record.evidence_id, record)
            elif isinstance(record, ContextEnrichmentTombstone):
                tombstones.setdefault(record.tombstone_id, record)
            else:
                raise ValueError("unsupported enrichment record")
        return ContextEnrichmentLedger(tuple(evidence.values()), tuple(tombstones.values()))


@dataclass(frozen=True, slots=True)
class ContextEnrichmentView:
    item: ContextItem
    current: tuple[ContextEnrichmentEvidence, ...]
    stale: tuple[ContextEnrichmentEvidence, ...]
    tombstoned: tuple[ContextEnrichmentEvidence, ...]


def project_context_enrichment(item: ContextItem, ledger: ContextEnrichmentLedger,
                               *, observed_at_ms: int) -> ContextEnrichmentView:
    now = _timestamp(observed_at_ms, "observed_at_ms")
    authority = context_authority(item)
    relevant = [x for x in ledger.evidence if authority in x.authorities]
    tombstoned_ids = {x.target_evidence_id for x in ledger.tombstones
                      if x.observed_at_ms <= now}
    tombstoned = tuple(x for x in relevant if x.evidence_id in tombstoned_ids)
    stale = tuple(x for x in relevant if x.evidence_id not in tombstoned_ids and
                  x.valid_until_ms < now)
    current = tuple(x for x in relevant if x.evidence_id not in tombstoned_ids and
                    x.observed_at_ms <= now <= x.valid_until_ms)
    return ContextEnrichmentView(item, current, stale, tombstoned)
