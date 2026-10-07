import hashlib
from pathlib import Path

import pytest

from vera.context_provider import ContextCitation
from vera.discovery_benchmark import (
    BenchmarkAuthority, BenchmarkHit, ContextBenchmarkCase,
    ContextBenchmarkFixture, ContextBenchmarkGate, ContextBenchmarkObservation,
    ContextBenchmarkVariant, compare_context_benchmark,
)
from vera.discovery_contract import DiscoveryRequest
from vera.fabric.dataset_provider import DatasetSnapshot


def digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def snapshot(source="fixed-corpus"):
    return DatasetSnapshot.create(
        dataset_id="context-benchmark", created_at="2026-09-28T10:00:00Z",
        records=({"record_id": "a", "revision": "r1"},),
        schema={"record_id": "string", "revision": "string"},
        provenance={"source": source})[0]


def case(snap, key="case-a"):
    request = DiscoveryRequest(
        query="private query " + key, requester="benchmark.runner",
        tenant_id="tenant.one", namespace="benchmark.context",
        as_of="2026-09-28T10:00:00Z", source_kinds=("api",),
        max_sources=4, max_context_items=8, timeout_ms=1_000,
        max_bytes=10_000, max_cost_units=10)
    return ContextBenchmarkCase(
        case_key=key, request=request,
        query_digest=digest("private query " + key), snapshot_id=snap.snapshot_id,
        relevant_sources=(f"source-{key}", "shared-source"),
        relevant_authorities=(BenchmarkAuthority(f"record:{key}:1", "r1"),
                              BenchmarkAuthority(f"record:{key}:2", "r2")),
        required_claim_ids=(digest(key + " claim 1"), digest(key + " claim 2")))


def variant(name, ablations=()):
    return ContextBenchmarkVariant(name, "route-r1", digest(name + " config"),
                                   ablations)


def hit(identity, revision):
    return BenchmarkHit(BenchmarkAuthority(identity, revision),
                        (ContextCitation("receipt", "fabric://record"),))


def observation(test_case, version, repetition, *, candidate=False,
                first_ms=None, end_ms=None, hits=None, sources=None):
    relevant = test_case.relevant_authorities
    if hits is None:
        hits = ((hit(relevant[0].identity, relevant[0].revision),
                 hit(relevant[1].identity, relevant[1].revision)) if candidate else
                (hit("record:irrelevant", "r1"),
                 hit(relevant[0].identity, relevant[0].revision)))
    if sources is None:
        sources = test_case.relevant_sources if candidate else test_case.relevant_sources[:1]
    first_ms = first_ms if first_ms is not None else (60 + repetition * 5 if candidate
                                                       else 100 + repetition * 10)
    end_ms = end_ms if end_ms is not None else first_ms + 20
    claims = test_case.required_claim_ids if candidate else test_case.required_claim_ids[:1]
    return ContextBenchmarkObservation(
        test_case.case_id, version.variant_id, repetition, "completed",
        discovered_sources=test_case.relevant_sources,
        selected_sources=sources, hits=hits, supported_claim_ids=claims,
        scout_ms=10, first_useful_context_ms=first_ms, end_to_end_ms=end_ms,
        byte_count=100, cost_units=1, cpu_ms=20,
        gpu_ms=5 if candidate else 0)


def fixture(*, regress_case=""):
    snap = snapshot()
    cases = (case(snap, "case-a"), case(snap, "case-b"))
    baseline = variant("baseline")
    candidate = variant("candidate", ("without-jepa",))
    observations = []
    for current in cases:
        for repetition in (1, 2):
            observations.append(observation(current, baseline, repetition))
            if current.case_key == regress_case:
                observations.append(observation(
                    current, candidate, repetition, candidate=True,
                    first_ms=140, end_ms=160,
                    hits=(hit("record:irrelevant", "r1"),)))
            else:
                observations.append(observation(
                    current, candidate, repetition, candidate=True))
    return ContextBenchmarkFixture(snap, cases, (candidate, baseline),
                                   tuple(observations), 2)


@pytest.mark.critical
def test_candidate_must_improve_speed_and_relevance_without_quality_regression():
    value = fixture()
    report = compare_context_benchmark(value, "baseline", "candidate")
    assert report["passed"] is True
    assert report["blockers"] == []
    baseline = report["summaries"]["baseline"]
    candidate = report["summaries"]["candidate"]
    assert candidate["latency_ms"]["first_useful_p95"] < baseline["latency_ms"]["first_useful_p95"]
    assert candidate["quality"]["ndcg"] > baseline["quality"]["ndcg"]
    assert candidate["quality"]["mrr"] >= baseline["quality"]["mrr"]
    assert candidate["quality"]["citation_coverage"] == 1.0
    assert candidate["quality"]["answer_support"] == 1.0
    assert candidate["resources"]["gpu_ms"] == 20
    assert report["effect"] == "none"
    assert report["providers_invoked"] is False


@pytest.mark.critical
def test_per_case_evidence_prevents_aggregate_from_hiding_regression():
    report = compare_context_benchmark(fixture(regress_case="case-b"),
                                       "baseline", "candidate",
                                       gate=ContextBenchmarkGate(
                                           minimum_p95_speed_improvement=0,
                                           minimum_ndcg_improvement=0,
                                           maximum_quality_regression=1))
    assert report["passed"] is False
    assert any(value.startswith("case_relevance_regressed:")
               for value in report["blockers"])
    assert any(value.startswith("case_latency_regressed:")
               for value in report["blockers"])


def test_report_is_payload_free_but_retains_cases_observations_and_ablations():
    report = compare_context_benchmark(fixture(), "baseline", "candidate")
    encoded = repr(report)
    assert "private query" not in encoded
    assert "cited result" not in encoded
    assert len(report["per_case"]) == 2
    assert len(report["case_results"]) == 2
    assert all(len(rows) == 2 for variants in report["case_results"].values()
               for rows in variants.values())
    assert len(report["observation_ids"]) == 8
    assert report["variants"]["candidate"]["ablations"] == ["without-jepa"]


def test_fixture_requires_exact_cartesian_matrix_and_snapshot():
    value = fixture()
    with pytest.raises(ValueError, match="exact case/variant/repetition matrix"):
        ContextBenchmarkFixture(value.snapshot, value.cases, value.variants,
                                value.observations[:-1], value.repetitions)
    wrong = case(snapshot("different"))
    with pytest.raises(ValueError, match="exact fixture snapshot"):
        ContextBenchmarkFixture(value.snapshot, (wrong,), value.variants,
                                value.observations, value.repetitions)


def test_case_binds_exact_request_query_digest_and_unambiguous_authority():
    snap = snapshot()
    value = case(snap)
    with pytest.raises(ValueError, match="does not match"):
        ContextBenchmarkCase(
            value.case_key, value.request, digest("other"), value.snapshot_id,
            value.relevant_sources, value.relevant_authorities,
            value.required_claim_ids)


def test_fixture_rejects_claim_support_outside_fixed_case_truth():
    value = fixture()
    original = value.observations[0]
    forged = ContextBenchmarkObservation(
        case_id=original.case_id, variant_id=original.variant_id,
        repetition=original.repetition, status=original.status,
        discovered_sources=original.discovered_sources,
        selected_sources=original.selected_sources, hits=original.hits,
        supported_claim_ids=(digest("not in truth"),),
        scout_ms=original.scout_ms,
        first_useful_context_ms=original.first_useful_context_ms,
        end_to_end_ms=original.end_to_end_ms,
        byte_count=original.byte_count, cost_units=original.cost_units,
        cpu_ms=original.cpu_ms, gpu_ms=original.gpu_ms)
    observations = (forged,) + value.observations[1:]
    with pytest.raises(ValueError, match="outside case truth"):
        ContextBenchmarkFixture(value.snapshot, value.cases, value.variants,
                                observations, value.repetitions)
    current = value.cases[0]
    with pytest.raises(ValueError, match="identities must be unique"):
        ContextBenchmarkCase(
            current.case_key, current.request, current.query_digest,
            current.snapshot_id, current.relevant_sources,
            (BenchmarkAuthority("same", "r1"), BenchmarkAuthority("same", "r2")),
            current.required_claim_ids)


def test_failed_observations_are_not_scored_as_zero_quality():
    value = fixture()
    failed = ContextBenchmarkObservation(
        value.cases[0].case_id, "candidate", 1, "timed_out",
        error_code="deadline_exceeded")
    assert failed.first_useful_context_ms is None
    with pytest.raises(ValueError, match="unfinished observation"):
        ContextBenchmarkObservation(
            value.cases[0].case_id, "candidate", 1, "failed",
            discovered_sources=("source",), error_code="failed")


@pytest.mark.parametrize("change", [
    {"scout_ms": 20, "end_to_end_ms": 10},
    {"first_useful_context_ms": 30, "end_to_end_ms": 20},
])
def test_completed_timing_order_is_fail_closed(change):
    value = fixture()
    current = value.cases[0]
    base = dict(case_id=current.case_id, variant_id="candidate", repetition=1,
                status="completed", discovered_sources=("source",),
                selected_sources=("source",), scout_ms=10, end_to_end_ms=20)
    base.update(change)
    with pytest.raises(ValueError, match="latency"):
        ContextBenchmarkObservation(**base)


def test_gate_rejects_nan_and_comparison_requires_distinct_known_variants():
    with pytest.raises(ValueError, match="between zero and one"):
        ContextBenchmarkGate(minimum_ndcg_improvement=float("nan"))
    value = fixture()
    with pytest.raises(ValueError, match="distinct known"):
        compare_context_benchmark(value, "baseline", "baseline")


def test_malformed_fixture_and_hit_fail_with_bounded_validation_errors():
    value = fixture()
    with pytest.raises(ValueError, match="fixture cases"):
        ContextBenchmarkFixture(value.snapshot, (object(),), value.variants,
                                value.observations, value.repetitions)
    with pytest.raises(ValueError, match="bounded citations"):
        BenchmarkHit(BenchmarkAuthority("record", "r1"), (object(),))


def test_policy_regression_is_a_separate_gate_dimension():
    value = fixture()
    observations = list(value.observations)
    index = next(index for index, observation in enumerate(observations)
                 if observation.variant_id == "candidate")
    original = observations[index]
    observations[index] = ContextBenchmarkObservation(
        case_id=original.case_id, variant_id=original.variant_id,
        repetition=original.repetition, status=original.status,
        discovered_sources=original.discovered_sources,
        selected_sources=original.selected_sources, hits=original.hits,
        supported_claim_ids=original.supported_claim_ids,
        policy_violations=("unsafe_source",), scout_ms=original.scout_ms,
        first_useful_context_ms=original.first_useful_context_ms,
        end_to_end_ms=original.end_to_end_ms,
        byte_count=original.byte_count, cost_units=original.cost_units,
        cpu_ms=original.cpu_ms, gpu_ms=original.gpu_ms)
    changed = ContextBenchmarkFixture(value.snapshot, value.cases, value.variants,
                                      tuple(observations), value.repetitions)
    report = compare_context_benchmark(changed, "baseline", "candidate")
    assert "policy_violations_regressed" in report["blockers"]


def test_duplicate_relevant_hits_cannot_inflate_ndcg_above_one():
    value = fixture()
    current = value.cases[0]
    repeated = hit(current.relevant_authorities[0].identity,
                   current.relevant_authorities[0].revision)
    duplicate = observation(current, value.variants[0], 1, candidate=True,
                            hits=(repeated, repeated, repeated))
    from vera.discovery_benchmark import _score
    assert 0 <= _score(current, duplicate)["ndcg"] <= 1


def test_benchmark_source_has_no_runtime_provider_or_model_dependency():
    source = (Path(__file__).parents[1] / "vera" /
              "discovery_benchmark.py").read_text().lower()
    for forbidden in ("requests", "httpx", "capability_registry", "ollama",
                      "torch", "tensorflow", "asyncio", "subprocess"):
        assert forbidden not in source
