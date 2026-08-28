"""Bounded, payload-free evidence windows for Worldview shadow parity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from .worldview_shadow_parity import WorldviewShadowParityReport


WORLDVIEW_SHADOW_EVIDENCE_SCHEMA = "vera.worldview-shadow-evidence/v1"
MAX_EVIDENCE_SAMPLES = 1_000
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class WorldviewShadowEvidenceEntry:
    observed_at: str
    manifest_id: str
    legacy_snapshot_hash: str
    ready: bool
    expected_records: int
    observed_records: int
    matched_records: int
    missing_records: int
    unexpected_records: int
    invalid_embeddings: int
    missing_revision_evidence: int
    drifted_revisions: int
    dangling_edges: int

    @classmethod
    def from_report(cls, report: WorldviewShadowParityReport,
                    *, observed_at: str) -> "WorldviewShadowEvidenceEntry":
        if not isinstance(report, WorldviewShadowParityReport):
            raise TypeError("report must be WorldviewShadowParityReport")
        try:
            parsed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if not report.manifest_id or len(report.manifest_id) > 256:
            raise ValueError("manifest_id must be a bounded non-empty string")
        if not _SHA256.fullmatch(report.legacy_snapshot_hash):
            raise ValueError("legacy_snapshot_hash must be a canonical sha256 identity")
        return cls(
            observed_at=observed_at, manifest_id=report.manifest_id,
            legacy_snapshot_hash=report.legacy_snapshot_hash,
            ready=report.ready_for_shadow_comparison,
            expected_records=report.expected_records,
            observed_records=report.observed_records,
            matched_records=report.matched_records,
            missing_records=len(report.missing_record_ids),
            unexpected_records=len(report.unexpected_record_ids),
            invalid_embeddings=len(report.invalid_embedding_record_ids),
            missing_revision_evidence=len(report.missing_revision_evidence_record_ids),
            drifted_revisions=len(report.drifted_revision_record_ids),
            dangling_edges=report.dangling_edge_count)


@dataclass(frozen=True)
class WorldviewShadowEvidenceSummary:
    samples: int
    ready_samples: int
    consecutive_ready_samples: int
    latest_ready: bool
    failure_sample_counts: tuple[tuple[str, int], ...]
    schema: str = WORLDVIEW_SHADOW_EVIDENCE_SCHEMA

    def to_dict(self) -> dict:
        return {**self.__dict__,
                "failure_sample_counts": dict(self.failure_sample_counts)}


@dataclass(frozen=True)
class WorldviewShadowEvidenceWindow:
    entries: tuple[WorldviewShadowEvidenceEntry, ...] = ()
    capacity: int = 100

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.capacity > MAX_EVIDENCE_SAMPLES:
            raise ValueError("evidence capacity is outside the supported limit")
        if len(self.entries) > self.capacity:
            raise ValueError("evidence entries exceed capacity")

    def append(self, entry: WorldviewShadowEvidenceEntry) -> "WorldviewShadowEvidenceWindow":
        if not isinstance(entry, WorldviewShadowEvidenceEntry):
            raise TypeError("entry must be WorldviewShadowEvidenceEntry")
        return WorldviewShadowEvidenceWindow(
            entries=(self.entries + (entry,))[-self.capacity:],
            capacity=self.capacity)

    def summary(self) -> WorldviewShadowEvidenceSummary:
        fields = (
            "missing_records", "unexpected_records", "invalid_embeddings",
            "missing_revision_evidence", "drifted_revisions", "dangling_edges")
        failures = tuple((name, sum(bool(getattr(item, name)) for item in self.entries))
                         for name in fields)
        consecutive = 0
        for item in reversed(self.entries):
            if not item.ready:
                break
            consecutive += 1
        return WorldviewShadowEvidenceSummary(
            samples=len(self.entries),
            ready_samples=sum(item.ready for item in self.entries),
            consecutive_ready_samples=consecutive,
            latest_ready=bool(self.entries and self.entries[-1].ready),
            failure_sample_counts=failures)
