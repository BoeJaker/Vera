"""Frozen relevance and contamination evaluation for Worldview lineages.

The evaluator consumes only revision/hash manifests.  Rankings and labels are
frozen fixtures supplied by the caller; no model, retriever, provider or live
dataset is invoked.  Its output is conformance evidence and can never authorize
JEPA influence or a consumer-route change.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from ..godseye.portable_dataset import make_portable_dataset
from .lineage_dataset_projection import (
    LineageDatasetProjection, project_lineage_dataset,
    verify_lineage_projection,
)


SCHEMA = "vera.worldview-lineage-shadow-evaluation/v1"
CORPUS_SCHEMA = "vera.worldview-lineage-shadow-corpus/v1"
LINEAGES = frozenset({
    "worldview.jepa", "worldview.non-jepa", "godseye.integrated",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/|-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation inputs must be canonical JSON") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    lineage: str
    record_id: str
    revision_id: str
    content_hash: str
    projection_id: str = ""

    def __post_init__(self) -> None:
        if self.lineage not in LINEAGES:
            raise ValueError("candidate lineage is unsupported")
        for name in ("record_id", "revision_id"):
            _identifier(getattr(self, name), name)
        if not _DIGEST.fullmatch(self.content_hash):
            raise ValueError("candidate content_hash must be sha256 evidence")
        if self.lineage == "worldview.jepa":
            if self.projection_id:
                raise ValueError("JEPA baseline candidates do not use lineage projections")
        elif not self.projection_id.startswith("wvlproj_"):
            raise ValueError("cross-lineage candidates require a projection identity")

    @property
    def candidate_id(self) -> str:
        return f"{self.lineage}|{self.record_id}"

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "lineage": self.lineage,
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "projection_id": self.projection_id,
        }


@dataclass(frozen=True, slots=True)
class FrozenLineageCase:
    case_id: str
    baseline_ranked: tuple[str, ...]
    enriched_ranked: tuple[str, ...]
    relevance: tuple[tuple[str, int], ...]
    allowed_lineages: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if not self.baseline_ranked or not self.enriched_ranked:
            raise ValueError("each case requires baseline and enriched rankings")
        for ranking in (self.baseline_ranked, self.enriched_ranked):
            if len(ranking) != len(set(ranking)):
                raise ValueError("rankings must not contain duplicate candidates")
            for candidate_id in ranking:
                _identifier(candidate_id, "candidate_id")
        relevance_ids = [candidate_id for candidate_id, _ in self.relevance]
        if not relevance_ids or len(relevance_ids) != len(set(relevance_ids)):
            raise ValueError("relevance labels must be present and unique")
        for candidate_id, grade in self.relevance:
            _identifier(candidate_id, "relevance candidate_id")
            if isinstance(grade, bool) or not isinstance(grade, int) or not 0 <= grade <= 3:
                raise ValueError("relevance grades must be integers from zero to three")
        if (not self.allowed_lineages or
                len(self.allowed_lineages) != len(set(self.allowed_lineages)) or
                any(value not in LINEAGES for value in self.allowed_lineages)):
            raise ValueError("allowed_lineages must be unique known lineages")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "baseline_ranked": list(self.baseline_ranked),
            "enriched_ranked": list(self.enriched_ranked),
            "relevance": [
                {"candidate_id": candidate_id, "grade": grade}
                for candidate_id, grade in self.relevance
            ],
            "allowed_lineages": list(self.allowed_lineages),
        }


def _projection_candidates(
        projection: LineageDatasetProjection) -> tuple[CandidateEvidence, ...]:
    if not isinstance(projection, LineageDatasetProjection):
        raise TypeError("projections must be LineageDatasetProjection values")
    verify_lineage_projection(projection)
    if projection.authority != "candidate_evidence_only":
        raise ValueError("projection authority is not eligible for shadow evaluation")
    hashes = dict(projection.record_content_hashes)
    if set(hashes) != {record_id for record_id, _ in projection.record_revisions}:
        raise ValueError("projection revision and content-hash sets differ")
    return tuple(CandidateEvidence(
        lineage=projection.source_lineage,
        record_id=record_id,
        revision_id=revision_id,
        content_hash=hashes[record_id],
        projection_id=projection.projection_id,
    ) for record_id, revision_id in projection.record_revisions)


def _dcg(ranking: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    return sum((2 ** relevance.get(candidate_id, 0) - 1) /
               math.log2(index + 2)
               for index, candidate_id in enumerate(ranking[:k]))


def _ndcg(ranking: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    ideal = sorted(relevance.values(), reverse=True)[:k]
    denominator = sum((2 ** grade - 1) / math.log2(index + 2)
                      for index, grade in enumerate(ideal))
    return _dcg(ranking, relevance, k) / denominator if denominator else 0.0


def _recall(ranking: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    relevant = {candidate_id for candidate_id, grade in relevance.items() if grade > 0}
    return (len(relevant.intersection(ranking[:k])) / len(relevant)) if relevant else 0.0


def evaluate_lineage_shadow(
        *, cases: Sequence[FrozenLineageCase],
        jepa_candidates: Sequence[CandidateEvidence],
        projections: Sequence[LineageDatasetProjection],
        k: int = 3) -> dict[str, Any]:
    """Evaluate supplied frozen rankings; never produce or alter a ranking."""
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
        raise ValueError("k must be an integer from one to 100")
    cases = tuple(cases)
    if (not cases or len(cases) > 1000 or
            not all(isinstance(case, FrozenLineageCase) for case in cases) or
            len({case.case_id for case in cases}) != len(cases)):
        raise ValueError("cases must contain 1..1000 unique frozen cases")
    candidates = tuple(jepa_candidates)
    if any(item.lineage != "worldview.jepa" for item in candidates):
        raise ValueError("jepa_candidates must belong to JEPA Worldview")
    for projection in projections:
        candidates += _projection_candidates(projection)
    registry = {item.candidate_id: item for item in candidates}
    if len(registry) != len(candidates):
        raise ValueError("candidate evidence identities must be unique")

    rows = []
    for case in sorted(cases, key=lambda value: value.case_id):
        relevance = dict(case.relevance)
        referenced = set(case.baseline_ranked) | set(case.enriched_ranked) | set(relevance)
        missing = sorted(referenced - set(registry))
        if missing:
            raise ValueError(f"case references unknown candidate evidence: {missing[0]}")
        if any(registry[item].lineage != "worldview.jepa"
               for item in case.baseline_ranked):
            raise ValueError("baseline rankings must contain only JEPA Worldview evidence")
        baseline_ndcg = _ndcg(case.baseline_ranked, relevance, k)
        enriched_ndcg = _ndcg(case.enriched_ranked, relevance, k)
        allowed = set(case.allowed_lineages)
        enriched_top = case.enriched_ranked[:k]
        contamination = sum(registry[item].lineage not in allowed
                            for item in enriched_top) / len(enriched_top)
        rows.append({
            "case_id": case.case_id,
            "baseline_ndcg": baseline_ndcg,
            "enriched_ndcg": enriched_ndcg,
            "ndcg_delta": enriched_ndcg - baseline_ndcg,
            "baseline_recall": _recall(case.baseline_ranked, relevance, k),
            "enriched_recall": _recall(case.enriched_ranked, relevance, k),
            "contamination_rate": contamination,
            "revision_hash_coverage": 1.0,
            "ranked_candidates": len(enriched_top),
        })

    corpus = {
        "schema": CORPUS_SCHEMA,
        "k": k,
        "cases": [case.to_dict() for case in sorted(cases, key=lambda value: value.case_id)],
        "candidates": [item.to_dict() for item in sorted(
            candidates, key=lambda value: value.candidate_id)],
    }
    mean = lambda field: sum(row[field] for row in rows) / len(rows)
    result = {
        "schema": SCHEMA,
        "corpus_id": _digest(corpus),
        "evidence_scope": "frozen_fixture_conformance",
        "k": k,
        "case_count": len(rows),
        "candidate_count": len(candidates),
        "cases": rows,
        "metrics": {
            "baseline_ndcg_mean": mean("baseline_ndcg"),
            "enriched_ndcg_mean": mean("enriched_ndcg"),
            "ndcg_delta_mean": mean("ndcg_delta"),
            "baseline_recall_mean": mean("baseline_recall"),
            "enriched_recall_mean": mean("enriched_recall"),
            "contamination_rate_mean": mean("contamination_rate"),
            "revision_hash_coverage_mean": mean("revision_hash_coverage"),
        },
        "decision": "insufficient_for_production",
        "production_ready": False,
        "authority_changed": False,
        "consumer_routing_changed": False,
        "models_invoked": False,
        "providers_invoked": False,
        "uses_accelerators": False,
        "mutates": False,
    }
    return {"evaluation_id": "wvleval_" + hashlib.sha256(
        _canonical(result).encode()).hexdigest(), **result}


def frozen_lineage_shadow_fixture() -> dict[str, Any]:
    """Return the versioned synthetic corpus and its deterministic evaluation."""
    godseye = project_lineage_dataset(make_portable_dataset(
        kind="imagery", source="godseye", source_revision="fixture-g1",
        created_at="2026-10-18T00:00:00Z",
        records=[{"id": "g1", "lat": 51.5, "lng": -0.1},
                 {"id": "g2", "lat": 51.6, "lng": -0.2}],
    ))
    non_jepa = project_lineage_dataset(make_portable_dataset(
        kind="buildings", source="worldview-non-jepa",
        source_revision="fixture-w1", created_at="2026-10-18T00:00:00Z",
        records=[{"id": "n1", "coords": [-0.1, 51.5, -0.2, 51.5, -0.2, 51.6]},
                 {"id": "n2", "coords": [-1.1, 52.5, -1.2, 52.5, -1.2, 52.6]}],
    ))
    jepa = tuple(CandidateEvidence(
        lineage="worldview.jepa", record_id=f"j{index}",
        revision_id=f"fixture-j{index}",
        content_hash=_digest({"fixture": "jepa", "index": index}),
    ) for index in range(1, 4))
    key = lambda lineage, record_id: f"{lineage}|{record_id}"
    j1, j2, j3 = (key("worldview.jepa", f"j{index}") for index in range(1, 4))
    g1, g2 = key("godseye.integrated", "g1"), key("godseye.integrated", "g2")
    n1, n2 = key("worldview.non-jepa", "n1"), key("worldview.non-jepa", "n2")
    cases = (
        FrozenLineageCase(
            "camera-coverage", (j1, j2, j3), (g1, n1, j1),
            ((j1, 1), (g1, 3), (n1, 2)), tuple(sorted(LINEAGES))),
        FrozenLineageCase(
            "building-change", (j2, j1, j3), (n2, j2, j1),
            ((j2, 1), (n2, 3)),
            ("worldview.jepa", "worldview.non-jepa")),
        FrozenLineageCase(
            "lineage-contamination-guard", (j3, j1, j2), (j3, g2, j1),
            ((j3, 3),), ("worldview.jepa",)),
    )
    return evaluate_lineage_shadow(
        cases=cases, jepa_candidates=jepa,
        projections=(godseye, non_jepa), k=3)
