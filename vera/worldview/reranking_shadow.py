"""Non-authoritative comparison of JEPA Worldview reranking evidence.

The seam stops before context selection. It accepts portable, already-produced
evidence, proves that every observation cites one candidate at the exact record
revision, and reports the ordering that a weighted score would have produced.
It never mutates candidates, invokes a model, or registers a ranker.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

from vera.context_provider import ContextItem
from vera.worldview.evidence_provider import WorldviewEvidence, evidence_availability


RERANKING_SHADOW_SCHEMA = "vera.worldview-reranking-shadow/v1"
MAX_SHADOW_CANDIDATES = 1000


def _candidate_id(item: ContextItem) -> str:
    return f"{item.provider}:{item.item_id}:{item.revision}"


def _order_key(item: ContextItem, score: float) -> tuple[Any, ...]:
    return (-score, item.token_count, item.provider, item.item_id, item.revision)


@dataclass(frozen=True, slots=True)
class RerankingShadowCandidate:
    """Payload-free comparison for one existing context candidate."""

    candidate_id: str
    provider: str
    item_id: str
    source_record_id: str
    revision_id: str
    baseline_rank: int
    shadow_rank: int
    baseline_score: float
    evidence_score: float | None
    hypothetical_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "item_id": self.item_id,
            "source_record_id": self.source_record_id,
            "revision_id": self.revision_id,
            "baseline_rank": self.baseline_rank,
            "shadow_rank": self.shadow_rank,
            "baseline_score": self.baseline_score,
            "evidence_score": self.evidence_score,
            "hypothetical_score": self.hypothetical_score,
        }


@dataclass(frozen=True, slots=True)
class RerankingShadowReport:
    """A comparison receipt with no authority over context composition."""

    status: str
    reason: str
    expected_snapshot_id: str
    expected_model_package_id: str
    evidence_id: str
    weight: float
    candidates: tuple[RerankingShadowCandidate, ...] = ()

    @property
    def matched_candidates(self) -> int:
        return sum(item.evidence_score is not None for item in self.candidates)

    @property
    def changed_positions(self) -> int:
        return sum(item.baseline_rank != item.shadow_rank for item in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RERANKING_SHADOW_SCHEMA,
            "mode": "shadow",
            "authoritative": False,
            "changes_context_selection": False,
            "status": self.status,
            "reason": self.reason,
            "expected_snapshot_id": self.expected_snapshot_id,
            "expected_model_package_id": self.expected_model_package_id,
            "evidence_id": self.evidence_id,
            "weight": self.weight,
            "candidate_count": len(self.candidates),
            "matched_candidates": self.matched_candidates,
            "changed_positions": self.changed_positions,
            "would_change_order": self.changed_positions > 0,
            "candidates": [item.to_dict() for item in self.candidates],
        }


def compare_reranking_shadow(
        items: Sequence[ContextItem], evidence: WorldviewEvidence | None, *,
        expected_snapshot_id: str, expected_model_package_id: str,
        weight: float = 0.25) -> RerankingShadowReport:
    """Compare exact-identity evidence without applying its result."""
    if isinstance(items, (str, bytes)):
        raise ValueError("context candidates must be a sequence")
    try:
        candidates = tuple(items)
    except TypeError as exc:
        raise ValueError("context candidates must be a sequence") from exc
    if len(candidates) > MAX_SHADOW_CANDIDATES:
        raise ValueError("context candidate limit exceeded")
    if not all(isinstance(item, ContextItem) for item in candidates):
        raise ValueError("shadow comparison requires valid ContextItems")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
            or not math.isfinite(weight) or not 0 <= weight <= 1:
        raise ValueError("reranking shadow weight must be between zero and one")
    for name, value in (
            ("expected_snapshot_id", expected_snapshot_id),
            ("expected_model_package_id", expected_model_package_id)):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a canonical identity")

    availability = evidence_availability(
        evidence, expected_snapshot_id=expected_snapshot_id,
        expected_model_package_id=expected_model_package_id)
    if not availability["usable"]:
        return RerankingShadowReport(
            status="ineligible", reason=availability["reason"],
            expected_snapshot_id=expected_snapshot_id,
            expected_model_package_id=expected_model_package_id,
            evidence_id=str(availability.get("evidence_id") or ""),
            weight=float(weight))
    assert evidence is not None
    if evidence.kind != "reranking":
        raise ValueError("shadow comparison requires reranking evidence")

    identities: dict[tuple[str, str], ContextItem] = {}
    for item in candidates:
        identity = (item.source, item.revision)
        if identity in identities:
            raise ValueError(
                "context source and revision identity must be unique for shadow comparison")
        identities[identity] = item

    scores: dict[tuple[str, str], float] = {}
    for observation in evidence.observations:
        if observation.score is None:
            raise ValueError("reranking observation requires a score")
        if len(observation.citations) != 1:
            raise ValueError("reranking observation requires exactly one record citation")
        citation = observation.citations[0]
        if observation.subject_id != f"record:{citation.record_id}":
            raise ValueError("reranking subject must match its cited record")
        identity = (citation.record_id, citation.revision_id)
        if identity not in identities:
            raise ValueError(
                "reranking evidence cites a record revision outside the candidate set")
        if identity in scores:
            raise ValueError("reranking evidence repeats a candidate record revision")
        scores[identity] = observation.score

    baseline = sorted(candidates, key=lambda item: _order_key(item, item.score))
    hypothetical: dict[int, float] = {}
    for item in candidates:
        evidence_score = scores.get((item.source, item.revision))
        hypothetical[id(item)] = item.score if evidence_score is None else (
            (1.0 - float(weight)) * item.score + float(weight) * evidence_score)
    shadow = sorted(candidates, key=lambda item: _order_key(item, hypothetical[id(item)]))
    baseline_ranks = {id(item): index + 1 for index, item in enumerate(baseline)}
    shadow_ranks = {id(item): index + 1 for index, item in enumerate(shadow)}

    compared = tuple(RerankingShadowCandidate(
        candidate_id=_candidate_id(item), provider=item.provider,
        item_id=item.item_id, source_record_id=item.source,
        revision_id=item.revision, baseline_rank=baseline_ranks[id(item)],
        shadow_rank=shadow_ranks[id(item)], baseline_score=item.score,
        evidence_score=scores.get((item.source, item.revision)),
        hypothetical_score=hypothetical[id(item)])
        for item in baseline)
    return RerankingShadowReport(
        status="compared", reason="exact_identity_match",
        expected_snapshot_id=expected_snapshot_id,
        expected_model_package_id=expected_model_package_id,
        evidence_id=evidence.evidence_id, weight=float(weight), candidates=compared)
