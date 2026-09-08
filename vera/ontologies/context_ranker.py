"""Optional ranking evidence from an explicit curated ontology snapshot."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import math

from vera.context_provider import ContextItem, ContextRankingEvidence

MAX_CURATED_ONTOLOGY_ASSERTIONS = 10_000


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"curated ontology {name} is required and must be canonical")
    return value


@dataclass(frozen=True, slots=True)
class CuratedOntologyAssertion:
    """One human-governed assertion in a caller-authorized snapshot."""
    assertion_id: str
    item_source: str
    concept_id: str
    relation: str
    confidence: float
    curator: str

    def __post_init__(self) -> None:
        for name in ("assertion_id", "item_source", "concept_id", "relation",
                     "curator"):
            _required(getattr(self, name), name)
        if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)) or not math.isfinite(
                    self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError(
                "curated ontology confidence must be between zero and one")


class CuratedOntologyContextRanker:
    """Rerank existing context without importing ontology content or authority."""

    def __init__(self, assertions: Sequence[CuratedOntologyAssertion], *,
                 ontology_id: str, ontology_revision: str,
                 weight: float = 0.15):
        ontology_id = _required(ontology_id, "id")
        ontology_revision = _required(ontology_revision, "revision")
        if isinstance(assertions, (str, bytes)) or not isinstance(
                assertions, Sequence):
            raise ValueError("curated ontology assertions must be a sequence")
        if len(assertions) > MAX_CURATED_ONTOLOGY_ASSERTIONS:
            raise ValueError("curated ontology assertion limit exceeded")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                or not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(
                "curated ontology ranking weight must be between zero and one")
        if any(not isinstance(value, CuratedOntologyAssertion)
               for value in assertions):
            raise ValueError("curated ontology assertion is invalid")
        assertion_ids = [value.assertion_id for value in assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("curated ontology assertion_id must be unique")

        strongest: dict[str, CuratedOntologyAssertion] = {}
        for assertion in assertions:
            existing = strongest.get(assertion.item_source)
            candidate_key = (assertion.confidence, assertion.assertion_id)
            existing_key = ((existing.confidence, existing.assertion_id)
                            if existing is not None else (-1.0, ""))
            if candidate_key > existing_key:
                strongest[assertion.item_source] = assertion
        self.ranker_id = f"ontology:{ontology_id}"
        self._revision = ontology_revision
        self._weight = float(weight)
        self._assertions = strongest

    def rank(self, items: Sequence[ContextItem]) -> tuple[ContextItem, ...]:
        ranked = []
        for item in items:
            assertion = self._assertions.get(item.source)
            if assertion is None:
                ranked.append(item)
                continue
            locator = f"{self.ranker_id}:{assertion.assertion_id}"
            if any(evidence.provider == self.ranker_id
                   and evidence.revision == self._revision
                   and evidence.locator == locator
                   for evidence in item.ranking_evidence):
                ranked.append(item)
                continue
            score = ((1.0 - self._weight) * item.score
                     + self._weight * assertion.confidence)
            evidence = ContextRankingEvidence(
                provider=self.ranker_id,
                revision=self._revision,
                score=float(assertion.confidence),
                weight=self._weight,
                locator=locator,
            )
            ranked.append(replace(
                item, score=score,
                ranking_evidence=item.ranking_evidence + (evidence,)))
        return tuple(ranked)
