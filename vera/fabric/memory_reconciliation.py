"""Bounded, non-mutating reconciliation for portable memory projections."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Sequence

from vera.fabric.dataset_provider import CancellationSignal
from vera.fabric.memory_provider import (
    MAX_MEMORY_PAGE,
    MemoryAccessContext,
    MemoryProjection,
    MemoryProvider,
)


MEMORY_RECONCILIATION_SCHEMA = "vera.memory-reconciliation/v1"
MAX_RECONCILE_RECORDS = 10_000
_MEMORY_ID = re.compile(r"^mem_[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^rec_[A-Za-z0-9._:-]{1,251}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class MemoryReconciliationReport:
    reconciliation_id: str
    provider: str
    tenant_id: str
    namespace: str
    provider_generation: int
    expected_count: int
    observed_count: int
    matched_count: int
    missing_memory_ids: tuple[str, ...]
    unexpected_memory_ids: tuple[str, ...]
    drifted_memory_ids: tuple[str, ...]
    expected_set_hash: str
    observed_set_hash: str
    schema: str = MEMORY_RECONCILIATION_SCHEMA

    @property
    def converged(self) -> bool:
        return not (self.missing_memory_ids or self.unexpected_memory_ids or
                    self.drifted_memory_ids)

    def to_dict(self) -> dict:
        return {**self.__dict__, "missing_memory_ids": list(self.missing_memory_ids),
                "unexpected_memory_ids": list(self.unexpected_memory_ids),
                "drifted_memory_ids": list(self.drifted_memory_ids),
                "converged": self.converged}


def _fingerprint(item: MemoryProjection | dict) -> dict:
    get = (lambda name: getattr(item, name)) if isinstance(
        item, MemoryProjection) else (lambda name: item.get(name))
    values = {"memory_id": get("memory_id"), "record_id": get("record_id"),
              "revision_id": get("revision_id"),
              "source_content_hash": get("source_content_hash"),
              "projection_hash": get("projection_hash"),
              "tombstone": get("tombstone")}
    for key, pattern in (("memory_id", _MEMORY_ID), ("record_id", _RECORD_ID),
                         ("revision_id", _REVISION_ID),
                         ("source_content_hash", _SHA256),
                         ("projection_hash", _SHA256)):
        if not isinstance(values[key], str) or not pattern.fullmatch(values[key]):
            raise ValueError(f"provider export has invalid {key}")
    if not isinstance(values["tombstone"], bool):
        raise ValueError("provider export has invalid tombstone")
    return values


def reconcile_memory_provider(
        provider: MemoryProvider, expected: Sequence[MemoryProjection],
        access: MemoryAccessContext, *, namespace: str = "",
        cancellation: CancellationSignal | None = None,
        page_size: int = MAX_MEMORY_PAGE) -> MemoryReconciliationReport:
    """Compare visible provider state with authoritative expected projections.

    The report contains identity and checksum evidence only. It does not repair,
    delete, apply, or disclose memory/query text.
    """
    signal = cancellation or CancellationSignal()
    expected_items = tuple(expected)
    if len(expected_items) > MAX_RECONCILE_RECORDS:
        raise ValueError("expected memory set exceeds reconciliation limit")
    page_size = int(page_size)
    if page_size < 1 or page_size > MAX_MEMORY_PAGE:
        raise ValueError(f"page_size must be between 1 and {MAX_MEMORY_PAGE}")
    expected_map: dict[str, dict] = {}
    for item in expected_items:
        signal.checkpoint()
        if not isinstance(item, MemoryProjection):
            raise TypeError("expected items must be MemoryProjection values")
        if item.tenant_id != access.tenant_id:
            raise ValueError("expected memory tenant does not match access tenant")
        if namespace and item.namespace != namespace:
            raise ValueError("expected memory namespace is outside reconciliation scope")
        if item.memory_id in expected_map:
            raise ValueError("expected memory IDs must be unique")
        expected_map[item.memory_id] = _fingerprint(item)

    observed_map: dict[str, dict] = {}
    cursor = ""
    generation: int | None = None
    export_id = ""
    while True:
        signal.checkpoint()
        page = provider.export(access, namespace=namespace, include_text=False,
                               limit=page_size, cursor=cursor,
                               cancellation=signal)
        if generation is None:
            generation, export_id = page.generation, page.export_id
        elif page.generation != generation or page.export_id != export_id:
            raise ValueError("memory provider changed during reconciliation")
        for item in page.projections:
            signal.checkpoint()
            fingerprint = _fingerprint(item)
            memory_id = str(fingerprint["memory_id"] or "")
            if memory_id in observed_map:
                raise ValueError("provider export contains duplicate memory IDs")
            observed_map[memory_id] = fingerprint
            if len(observed_map) > MAX_RECONCILE_RECORDS:
                raise ValueError("provider memory set exceeds reconciliation limit")
        cursor = page.next_cursor
        if not cursor:
            break

    expected_ids, observed_ids = set(expected_map), set(observed_map)
    missing = tuple(sorted(expected_ids - observed_ids))
    unexpected = tuple(sorted(observed_ids - expected_ids))
    drifted = tuple(sorted(memory_id for memory_id in expected_ids & observed_ids
                           if expected_map[memory_id] != observed_map[memory_id]))
    matched = len(expected_ids & observed_ids) - len(drifted)
    expected_hash = _digest([expected_map[key] for key in sorted(expected_map)])
    observed_hash = _digest([observed_map[key] for key in sorted(observed_map)])
    identity = {"schema": MEMORY_RECONCILIATION_SCHEMA,
                "provider": str(provider.name), "tenant_id": access.tenant_id,
                "namespace": namespace, "provider_generation": generation or 0,
                "expected_set_hash": expected_hash,
                "observed_set_hash": observed_hash}
    return MemoryReconciliationReport(
        reconciliation_id="mrec_" + hashlib.sha256(
            _canonical(identity).encode()).hexdigest(),
        provider=str(provider.name), tenant_id=access.tenant_id,
        namespace=namespace, provider_generation=generation or 0,
        expected_count=len(expected_map), observed_count=len(observed_map),
        matched_count=matched, missing_memory_ids=missing,
        unexpected_memory_ids=unexpected, drifted_memory_ids=drifted,
        expected_set_hash=expected_hash, observed_set_hash=observed_hash)
