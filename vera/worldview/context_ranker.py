"""Authority-preserving Worldview ranking for already-cited context."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import math

from vera.context_provider import ContextItem, ContextRankingEvidence

MAX_WORLDVIEW_RANKING_RESULTS = 1000


class WorldviewContextRanker:
    ranker_id = "worldview"

    def __init__(self, results: Sequence[Mapping], *, model_revision: str,
                 weight: float = 0.25):
        model_revision = str(model_revision or "").strip()
        if not model_revision:
            raise ValueError("Worldview model_revision is required")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                or not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(
                "Worldview ranking weight must be between zero and one")
        if len(results) > MAX_WORLDVIEW_RANKING_RESULTS:
            raise ValueError("Worldview ranking result limit exceeded")
        scores: dict[str, float] = {}
        for result in results:
            if not isinstance(result, Mapping):
                raise ValueError("Worldview ranking result must be an object")
            record_id = str(result.get("id") or "").strip()
            raw_score = result.get("score")
            if not record_id:
                raise ValueError("Worldview ranking result requires id")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)) \
                    or not math.isfinite(raw_score) or not -1 <= raw_score <= 1:
                raise ValueError(
                    "Worldview cosine score must be between minus one and one")
            normalized = (float(raw_score) + 1.0) / 2.0
            scores[record_id] = max(normalized, scores.get(record_id, 0.0))
        self._scores = scores
        self._revision = model_revision
        self._weight = float(weight)

    def rank(self, items: Sequence[ContextItem]) -> tuple[ContextItem, ...]:
        ranked = []
        for item in items:
            worldview_score = self._scores.get(item.source)
            if worldview_score is None:
                ranked.append(item)
                continue
            if any(evidence.provider == self.ranker_id
                   and evidence.revision == self._revision
                   for evidence in item.ranking_evidence):
                ranked.append(item)
                continue
            score = ((1.0 - self._weight) * item.score
                     + self._weight * worldview_score)
            evidence = ContextRankingEvidence(
                self.ranker_id, self._revision, worldview_score, self._weight)
            ranked.append(replace(
                item, score=score,
                ranking_evidence=item.ranking_evidence + (evidence,)))
        return tuple(ranked)
