"""Offline, payload-free benchmark contracts for discovery and context routes."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

# Dual-spelled: in the running app the package is only importable as Vera.vera (the plain 'vera' is a test-path
# spelling), and a plain-only import here took operator_web_capabilities down with it on prod (2026-09-30).
try:  # the test path's spelling first; the app has only Vera.vera, so it takes the fallback - one spelling per process
    from vera.context_provider import ContextCitation
    from vera.discovery_contract import DiscoveryRequest
    from vera.fabric.dataset_provider import DatasetSnapshot
except ImportError:  # pragma: no cover - the running app
    from Vera.vera.context_provider import ContextCitation
    from Vera.vera.discovery_contract import DiscoveryRequest
    from Vera.vera.fabric.dataset_provider import DatasetSnapshot


SCHEMA = "vera.discovery-context-benchmark/v1"
MAX_CASES = 10_000
MAX_VARIANTS = 64
MAX_REPETITIONS = 1_000
MAX_HITS = 1_000
MAX_OBSERVATIONS = 1_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _id(value: object, name: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _digest(value: object, name: str) -> str:
    value = str(value or "").lower()
    if not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a sha256 digest")
    return value


def _count(value: object, name: str, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def _ratio(value: object, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f"{name} must be between zero and one")
    return float(value)


def _hash(prefix: str, value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode()
    return prefix + hashlib.sha256(raw).hexdigest()


def _percentile(values: Sequence[int], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True, slots=True, order=True)
class BenchmarkAuthority:
    identity: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity", _id(self.identity, "authority identity"))
        object.__setattr__(self, "revision", _id(self.revision, "authority revision"))

    def to_dict(self) -> dict[str, str]:
        return {"identity": self.identity, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class ContextBenchmarkCase:
    case_key: str
    request: DiscoveryRequest
    query_digest: str
    snapshot_id: str
    relevant_sources: tuple[str, ...]
    relevant_authorities: tuple[BenchmarkAuthority, ...]
    required_claim_ids: tuple[str, ...]
    case_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_key", _id(self.case_key, "case key"))
        if not isinstance(self.request, DiscoveryRequest):
            raise ValueError("case requires a DiscoveryRequest")
        object.__setattr__(self, "query_digest", _digest(
            self.query_digest, "query digest"))
        expected_query_digest = "sha256:" + hashlib.sha256(
            self.request.query.encode()).hexdigest()
        if self.query_digest != expected_query_digest:
            raise ValueError("query digest does not match DiscoveryRequest")
        if not re.fullmatch(r"snap_[0-9a-f]{64}", str(self.snapshot_id or "")):
            raise ValueError("snapshot_id must identify a DatasetSnapshot")
        sources = tuple(sorted({_id(value, "relevant source")
                                for value in self.relevant_sources}))
        try:
            authorities = tuple(self.relevant_authorities)
        except TypeError as exc:
            raise ValueError("case authorities must be a sequence") from exc
        claims = tuple(sorted({_digest(value, "claim ID")
                               for value in self.required_claim_ids}))
        if not sources or not authorities or not claims:
            raise ValueError("case truth requires sources, authorities and claims")
        if not authorities or len(authorities) > MAX_HITS or not all(
                isinstance(value, BenchmarkAuthority) for value in authorities):
            raise ValueError("case authorities are invalid or exceed the limit")
        authorities = tuple(sorted(authorities))
        if len(authorities) != len(set(authorities)):
            raise ValueError("case authorities must be unique")
        if len({value.identity for value in authorities}) != len(authorities):
            raise ValueError("case authority identities must be unique")
        object.__setattr__(self, "relevant_sources", sources)
        object.__setattr__(self, "relevant_authorities", authorities)
        object.__setattr__(self, "required_claim_ids", claims)
        object.__setattr__(self, "case_id", _hash("dcbcase_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "case_key": self.case_key, "request_id": self.request.request_id,
            "query_digest": self.query_digest, "snapshot_id": self.snapshot_id,
            "relevant_sources": list(self.relevant_sources),
            "relevant_authorities": [value.to_dict()
                                     for value in self.relevant_authorities],
            "required_claim_ids": list(self.required_claim_ids),
        }


@dataclass(frozen=True, slots=True)
class ContextBenchmarkVariant:
    variant_id: str
    revision: str
    configuration_digest: str
    ablations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variant_id", _id(self.variant_id, "variant ID"))
        object.__setattr__(self, "revision", _id(self.revision, "variant revision"))
        object.__setattr__(self, "configuration_digest", _digest(
            self.configuration_digest, "configuration digest"))
        ablations = tuple(sorted({_id(value, "ablation") for value in self.ablations}))
        object.__setattr__(self, "ablations", ablations)

    def to_dict(self) -> dict[str, Any]:
        return {"variant_id": self.variant_id, "revision": self.revision,
                "configuration_digest": self.configuration_digest,
                "ablations": list(self.ablations)}


@dataclass(frozen=True, slots=True)
class BenchmarkHit:
    authority: BenchmarkAuthority
    citations: tuple[ContextCitation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.authority, BenchmarkAuthority):
            raise ValueError("hit requires a benchmark authority")
        try:
            citations = tuple(self.citations)
        except TypeError as exc:
            raise ValueError("hit citations must be a sequence") from exc
        if not citations or len(citations) > 128 or not all(
                isinstance(value, ContextCitation) for value in citations):
            raise ValueError("hit requires bounded citations")
        citations = tuple(sorted(set(citations),
                                 key=lambda value: (value.source_id, value.locator)))
        object.__setattr__(self, "citations", citations)

    def to_dict(self) -> dict[str, Any]:
        return {"authority": self.authority.to_dict(),
                "citations": [{"source_id": value.source_id,
                               "locator": value.locator}
                              for value in self.citations]}


@dataclass(frozen=True, slots=True)
class ContextBenchmarkObservation:
    case_id: str
    variant_id: str
    repetition: int
    status: str
    discovered_sources: tuple[str, ...] = ()
    selected_sources: tuple[str, ...] = ()
    hits: tuple[BenchmarkHit, ...] = ()
    supported_claim_ids: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()
    scout_ms: int | None = None
    first_useful_context_ms: int | None = None
    end_to_end_ms: int | None = None
    byte_count: int = 0
    cost_units: int = 0
    cpu_ms: int = 0
    gpu_ms: int = 0
    error_code: str = ""
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _id(self.case_id, "case ID"))
        object.__setattr__(self, "variant_id", _id(self.variant_id, "variant ID"))
        if not 1 <= _count(self.repetition, "repetition", MAX_REPETITIONS) <= MAX_REPETITIONS:
            raise ValueError("repetition must be positive")
        if self.status not in {"completed", "failed", "cancelled", "timed_out"}:
            raise ValueError("unsupported observation status")
        sources = tuple(sorted({_id(value, "source") for value in self.discovered_sources}))
        selected = tuple(sorted({_id(value, "selected source")
                                 for value in self.selected_sources}))
        if not set(selected).issubset(set(sources)):
            raise ValueError("selected sources must have been discovered")
        hits = tuple(self.hits)
        if len(hits) > MAX_HITS or not all(isinstance(value, BenchmarkHit)
                                           for value in hits):
            raise ValueError("hits are invalid or exceed the limit")
        claims = tuple(sorted({_digest(value, "supported claim")
                               for value in self.supported_claim_ids}))
        violations = tuple(sorted({_id(value, "policy violation")
                                   for value in self.policy_violations}))
        for name in ("byte_count", "cost_units", "cpu_ms", "gpu_ms"):
            object.__setattr__(self, name, _count(getattr(self, name), name))
        timings = []
        for name in ("scout_ms", "first_useful_context_ms", "end_to_end_ms"):
            value = getattr(self, name)
            if value is not None:
                value = _count(value, name)
                object.__setattr__(self, name, value)
                timings.append((name, value))
        error = str(self.error_code or "").strip()
        if error:
            error = _id(error, "error code")
        if self.status == "completed":
            if self.scout_ms is None or self.end_to_end_ms is None or error:
                raise ValueError("completed observation requires timings and no error")
            if self.scout_ms > self.end_to_end_ms:
                raise ValueError("scout latency cannot exceed end-to-end latency")
            if (self.first_useful_context_ms is not None and
                    self.first_useful_context_ms > self.end_to_end_ms):
                raise ValueError("useful-context latency cannot exceed end-to-end latency")
        elif (sources or selected or hits or claims or violations or timings or
              self.byte_count or self.cost_units or self.cpu_ms or self.gpu_ms):
            raise ValueError("unfinished observation cannot claim results or resources")
        elif not error:
            raise ValueError("unfinished observation requires an error code")
        object.__setattr__(self, "discovered_sources", sources)
        object.__setattr__(self, "selected_sources", selected)
        object.__setattr__(self, "hits", hits)
        object.__setattr__(self, "supported_claim_ids", claims)
        object.__setattr__(self, "policy_violations", violations)
        object.__setattr__(self, "error_code", error)
        object.__setattr__(self, "observation_id", _hash("dcbobs_", self.to_dict(False)))

    def to_dict(self, include_id: bool = True) -> dict[str, Any]:
        value = {
            "case_id": self.case_id, "variant_id": self.variant_id,
            "repetition": self.repetition, "status": self.status,
            "discovered_sources": list(self.discovered_sources),
            "selected_sources": list(self.selected_sources),
            "hits": [item.to_dict() for item in self.hits],
            "supported_claim_ids": list(self.supported_claim_ids),
            "policy_violations": list(self.policy_violations),
            "scout_ms": self.scout_ms,
            "first_useful_context_ms": self.first_useful_context_ms,
            "end_to_end_ms": self.end_to_end_ms,
            "byte_count": self.byte_count, "cost_units": self.cost_units,
            "cpu_ms": self.cpu_ms, "gpu_ms": self.gpu_ms,
            "error_code": self.error_code,
        }
        return {"observation_id": self.observation_id, **value} if include_id else value


@dataclass(frozen=True, slots=True)
class ContextBenchmarkFixture:
    snapshot: DatasetSnapshot
    cases: tuple[ContextBenchmarkCase, ...]
    variants: tuple[ContextBenchmarkVariant, ...]
    observations: tuple[ContextBenchmarkObservation, ...]
    repetitions: int
    fixture_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, DatasetSnapshot):
            raise ValueError("fixture requires DatasetSnapshot")
        repetitions = _count(self.repetitions, "repetitions", MAX_REPETITIONS)
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        try:
            cases = tuple(self.cases)
            variants = tuple(self.variants)
            observations = tuple(self.observations)
        except TypeError as exc:
            raise ValueError("fixture collections must be sequences") from exc
        if not cases or len(cases) > MAX_CASES or not all(
                isinstance(value, ContextBenchmarkCase) for value in cases):
            raise ValueError("fixture cases are invalid or empty")
        if len(variants) < 2 or len(variants) > MAX_VARIANTS or not all(
                isinstance(value, ContextBenchmarkVariant) for value in variants):
            raise ValueError("fixture requires two to 64 variants")
        if not all(isinstance(value, ContextBenchmarkObservation)
                   for value in observations):
            raise ValueError("fixture observations are invalid")
        if len(observations) > MAX_OBSERVATIONS:
            raise ValueError("fixture observations exceed the limit")
        cases = tuple(sorted(cases, key=lambda value: value.case_id))
        variants = tuple(sorted(variants, key=lambda value: value.variant_id))
        observations = tuple(sorted(observations, key=lambda value: (
            value.variant_id, value.case_id, value.repetition)))
        if any(value.snapshot_id != self.snapshot.snapshot_id for value in cases):
            raise ValueError("all cases must use the exact fixture snapshot")
        case_ids = {value.case_id for value in cases}
        variant_ids = {value.variant_id for value in variants}
        if len(case_ids) != len(cases) or len(variant_ids) != len(variants):
            raise ValueError("case and variant identities must be unique")
        expected = {(variant, case, repetition)
                    for variant in variant_ids for case in case_ids
                    for repetition in range(1, repetitions + 1)}
        actual = {(value.variant_id, value.case_id, value.repetition)
                  for value in observations}
        if actual != expected or len(actual) != len(observations):
            raise ValueError("fixture requires the exact case/variant/repetition matrix")
        case_index = {value.case_id: value for value in cases}
        for observation in observations:
            if not set(observation.supported_claim_ids).issubset(
                    set(case_index[observation.case_id].required_claim_ids)):
                raise ValueError("observation claims support outside case truth")
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "variants", variants)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "repetitions", repetitions)
        identity = {"schema": SCHEMA, "snapshot_id": self.snapshot.snapshot_id,
                    "case_ids": sorted(case_ids),
                    "variants": [value.to_dict() for value in variants],
                    "observation_ids": [value.observation_id for value in observations],
                    "repetitions": repetitions}
        object.__setattr__(self, "fixture_id", _hash("dcbfix_", identity))


@dataclass(frozen=True, slots=True)
class ContextBenchmarkGate:
    minimum_p95_speed_improvement: float = 0.05
    minimum_ndcg_improvement: float = 0.01
    maximum_case_latency_regression: float = 0.10
    maximum_case_ndcg_regression: float = 0.0
    maximum_quality_regression: float = 0.0
    maximum_unsuccessful_rate_regression: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _ratio(getattr(self, name), name))


def _ndcg(hits: Sequence[BenchmarkHit], relevant: set[str]) -> float:
    seen: set[str] = set()
    gains = []
    for value in hits:
        identity = value.authority.identity
        gain = identity in relevant and identity not in seen
        gains.append(1.0 if gain else 0.0)
        if gain:
            seen.add(identity)
    dcg = math.fsum(gain / math.log2(index + 2)
                    for index, gain in enumerate(gains))
    ideal = math.fsum(1 / math.log2(index + 2)
                      for index in range(min(len(relevant), len(hits))))
    return dcg / ideal if ideal else 0.0


def _score(case: ContextBenchmarkCase,
           observation: ContextBenchmarkObservation) -> dict[str, float | int | None]:
    if observation.status != "completed":
        return {"ndcg": None, "mrr": None, "source_recall": None,
                "source_precision": None, "citation_coverage": None,
                "answer_support": None, "freshness": None,
                "redundancy": None}
    relevant_sources = set(case.relevant_sources)
    selected = set(observation.selected_sources)
    source_hits = len(relevant_sources & selected)
    relevant_by_identity = {value.identity: value.revision
                            for value in case.relevant_authorities}
    relevant_ids = set(relevant_by_identity)
    ranked_hits = [value.authority.identity in relevant_ids
                   for value in observation.hits]
    relevant_returned = [value for value in observation.hits
                         if value.authority.identity in relevant_ids]
    exact_revisions = sum(relevant_by_identity[value.authority.identity] ==
                          value.authority.revision for value in relevant_returned)
    unique_hit_ids = {value.authority.identity for value in observation.hits}
    return {
        "ndcg": _ndcg(observation.hits, relevant_ids),
        "mrr": next((1 / (index + 1) for index, hit in enumerate(ranked_hits) if hit), 0.0),
        "source_recall": source_hits / len(relevant_sources),
        "source_precision": source_hits / len(selected) if selected else 0.0,
        "citation_coverage": (len({value.authority.identity
                                   for value in relevant_returned}) /
                              len(relevant_ids)),
        "answer_support": (len(set(observation.supported_claim_ids) &
                               set(case.required_claim_ids)) /
                           len(case.required_claim_ids)),
        "freshness": (exact_revisions / len(relevant_returned)
                      if relevant_returned else 0.0),
        "redundancy": (len(unique_hit_ids) / len(observation.hits)
                       if observation.hits else 1.0),
    }


def _aggregate(rows: Sequence[tuple[ContextBenchmarkObservation, dict[str, Any]]]
               ) -> dict[str, Any]:
    completed = [(observation, scores) for observation, scores in rows
                 if observation.status == "completed"]
    metrics = ("ndcg", "mrr", "source_recall", "source_precision",
               "citation_coverage", "answer_support", "freshness", "redundancy")
    return {
        "samples": len(rows), "completed": len(completed),
        "unsuccessful_rate": (len(rows) - len(completed)) / len(rows),
        "useful_context_rate": (sum(
            observation.first_useful_context_ms is not None
            for observation, _ in completed) / len(rows)),
        "quality": {name: (math.fsum(scores[name] for _, scores in completed) /
                           len(completed) if completed else None)
                    for name in metrics},
        "latency_ms": {
            "first_useful_p50": _percentile([
                value.first_useful_context_ms for value, _ in completed
                if value.first_useful_context_ms is not None], .50),
            "first_useful_p95": _percentile([
                value.first_useful_context_ms for value, _ in completed
                if value.first_useful_context_ms is not None], .95),
            "first_useful_p99": _percentile([
                value.first_useful_context_ms for value, _ in completed
                if value.first_useful_context_ms is not None], .99),
            "end_to_end_p95": _percentile([
                value.end_to_end_ms for value, _ in completed], .95),
        },
        "resources": {
            "bytes": sum(value.byte_count for value, _ in completed),
            "cost_units": sum(value.cost_units for value, _ in completed),
            "cpu_ms": sum(value.cpu_ms for value, _ in completed),
            "gpu_ms": sum(value.gpu_ms for value, _ in completed),
        },
        "policy_violations": sum(len(value.policy_violations)
                                 for value, _ in completed),
    }


def compare_context_benchmark(
    fixture: ContextBenchmarkFixture,
    baseline_variant_id: str,
    candidate_variant_id: str,
    *,
    gate: ContextBenchmarkGate = ContextBenchmarkGate(),
) -> dict[str, Any]:
    """Compare supplied observations only; never invokes a source or model."""
    if not isinstance(fixture, ContextBenchmarkFixture):
        raise ValueError("fixture must be ContextBenchmarkFixture")
    if not isinstance(gate, ContextBenchmarkGate):
        raise ValueError("gate must be ContextBenchmarkGate")
    baseline_variant_id = _id(baseline_variant_id, "baseline variant")
    candidate_variant_id = _id(candidate_variant_id, "candidate variant")
    variants = {value.variant_id: value for value in fixture.variants}
    if baseline_variant_id == candidate_variant_id or baseline_variant_id not in variants \
            or candidate_variant_id not in variants:
        raise ValueError("comparison requires two distinct known variants")
    cases = {value.case_id: value for value in fixture.cases}
    rows: dict[str, list[tuple[ContextBenchmarkObservation, dict[str, Any]]]] = {
        value.variant_id: [] for value in fixture.variants}
    per_case_rows: dict[tuple[str, str], list[tuple[ContextBenchmarkObservation,
                                                   dict[str, Any]]]] = {}
    for observation in fixture.observations:
        score = _score(cases[observation.case_id], observation)
        rows[observation.variant_id].append((observation, score))
        per_case_rows.setdefault((observation.variant_id, observation.case_id), []).append(
            (observation, score))
    summaries = {variant: _aggregate(value) for variant, value in rows.items()}
    per_case = {
        case_id: {
            baseline_variant_id: _aggregate(per_case_rows[(baseline_variant_id, case_id)]),
            candidate_variant_id: _aggregate(per_case_rows[(candidate_variant_id, case_id)]),
        } for case_id in sorted(cases)
    }
    baseline = summaries[baseline_variant_id]
    candidate = summaries[candidate_variant_id]
    blockers: list[str] = []
    base_p95 = baseline["latency_ms"]["first_useful_p95"]
    cand_p95 = candidate["latency_ms"]["first_useful_p95"]
    if base_p95 is None or cand_p95 is None:
        blockers.append("missing_useful_context_latency")
    elif cand_p95 > base_p95 * (1 - gate.minimum_p95_speed_improvement):
        blockers.append("p95_useful_context_speed_not_improved")
    base_ndcg = baseline["quality"]["ndcg"]
    cand_ndcg = candidate["quality"]["ndcg"]
    if base_ndcg is None or cand_ndcg is None:
        blockers.append("missing_relevance_evidence")
    elif cand_ndcg < base_ndcg + gate.minimum_ndcg_improvement:
        blockers.append("contextual_relevance_not_improved")
    for metric in ("mrr", "citation_coverage", "answer_support", "freshness",
                   "source_recall", "source_precision", "redundancy"):
        before, after = baseline["quality"][metric], candidate["quality"][metric]
        if before is None or after is None or after + gate.maximum_quality_regression < before:
            blockers.append(f"{metric}_regressed")
    if (candidate["unsuccessful_rate"] > baseline["unsuccessful_rate"] +
            gate.maximum_unsuccessful_rate_regression):
        blockers.append("unsuccessful_rate_regressed")
    if candidate["useful_context_rate"] < baseline["useful_context_rate"]:
        blockers.append("useful_context_rate_regressed")
    if candidate["policy_violations"] > baseline["policy_violations"]:
        blockers.append("policy_violations_regressed")
    for case_id, values in per_case.items():
        before, after = values[baseline_variant_id], values[candidate_variant_id]
        before_ndcg, after_ndcg = before["quality"]["ndcg"], after["quality"]["ndcg"]
        if (before_ndcg is None or after_ndcg is None or
                after_ndcg + gate.maximum_case_ndcg_regression < before_ndcg):
            blockers.append(f"case_relevance_regressed:{case_id}")
        before_p95 = before["latency_ms"]["first_useful_p95"]
        after_p95 = after["latency_ms"]["first_useful_p95"]
        if (before_p95 is None or after_p95 is None or
                after_p95 > before_p95 * (1 + gate.maximum_case_latency_regression)):
            blockers.append(f"case_latency_regressed:{case_id}")
    case_results: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for observation in fixture.observations:
        scores = _score(cases[observation.case_id], observation)
        case_results.setdefault(observation.case_id, {}).setdefault(
            observation.variant_id, []).append({
                "observation_id": observation.observation_id,
                "repetition": observation.repetition,
                "status": observation.status,
                "error_code": observation.error_code,
                "metrics": scores,
                "latency_ms": {
                    "scout": observation.scout_ms,
                    "first_useful_context": observation.first_useful_context_ms,
                    "end_to_end": observation.end_to_end_ms,
                },
                "resources": {
                    "bytes": observation.byte_count,
                    "cost_units": observation.cost_units,
                    "cpu_ms": observation.cpu_ms,
                    "gpu_ms": observation.gpu_ms,
                },
                "policy_violations": list(observation.policy_violations),
            })
    return {
        "schema": SCHEMA, "fixture_id": fixture.fixture_id,
        "snapshot_id": fixture.snapshot.snapshot_id,
        "baseline_variant_id": baseline_variant_id,
        "candidate_variant_id": candidate_variant_id,
        "variants": {value.variant_id: value.to_dict() for value in fixture.variants},
        "summaries": summaries, "per_case": per_case,
        "case_results": case_results,
        "observation_ids": [value.observation_id for value in fixture.observations],
        "gate": {name: getattr(gate, name) for name in gate.__dataclass_fields__},
        "blockers": sorted(set(blockers)), "passed": not blockers,
        "effect": "none", "providers_invoked": False,
    }
