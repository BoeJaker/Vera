import asyncio
import hashlib

import pytest

from vera.context_provider import ContextCitation
from vera.discovery_benchmark import (
    BenchmarkAuthority,
    BenchmarkHit,
    ContextBenchmarkCase,
    ContextBenchmarkVariant,
)
from vera.discovery_benchmark_runtime import (
    BenchmarkMilestones,
    ContextBenchmarkRuntime,
    LiveContextResult,
    LiveContextVariantRunner,
    snapshot_retrieval_runner,
)
from vera.discovery_contract import DiscoveryRequest
from vera.fabric.dataset_provider import DatasetSnapshot
from vera.fabric.retrieval_comparison import RetrievalProviderProfile


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def snapshot(source: str = "sealed-live-fixture") -> DatasetSnapshot:
    return DatasetSnapshot.create(
        dataset_id="live-context-benchmark",
        created_at="2026-10-01T10:00:00Z",
        records=({"record_id": "a", "revision": "r1"},),
        schema={"record_id": "string", "revision": "string"},
        provenance={"source": source},
    )[0]


def benchmark_case(value: DatasetSnapshot, key: str = "case-a") -> ContextBenchmarkCase:
    query = "sealed query " + key
    request = DiscoveryRequest(
        query=query,
        requester="benchmark.runner",
        tenant_id="tenant.one",
        namespace="benchmark.context",
        as_of="2026-10-01T10:00:00Z",
        source_kinds=("api",),
        max_sources=4,
        max_context_items=8,
        timeout_ms=1_000,
        max_bytes=10_000,
        max_cost_units=10,
    )
    return ContextBenchmarkCase(
        case_key=key,
        request=request,
        query_digest=digest(query),
        snapshot_id=value.snapshot_id,
        relevant_sources=("source-" + key,),
        relevant_authorities=(BenchmarkAuthority("record:" + key, "r1"),),
        required_claim_ids=(digest(key + " claim"),),
    )


def variant(name: str) -> ContextBenchmarkVariant:
    return ContextBenchmarkVariant(name, "route-r1", digest(name + " config"))


def result_for(case: ContextBenchmarkCase) -> LiveContextResult:
    authority = case.relevant_authorities[0]
    return LiveContextResult(
        discovered_sources=case.relevant_sources,
        selected_sources=case.relevant_sources,
        hits=(BenchmarkHit(authority, (ContextCitation("receipt", "fabric://record"),)),),
        supported_claim_ids=case.required_claim_ids,
        byte_count=100,
        cost_units=1,
        cpu_ms=3,
    )


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_executes_exact_sequential_matrix_with_payload_free_receipts():
    snap = snapshot()
    cases = (benchmark_case(snap, "case-a"), benchmark_case(snap, "case-b"))
    calls = []

    def runner(name):
        async def execute(current, milestones):
            calls.append((name, current.case_key))
            milestones.scout_complete()
            await asyncio.sleep(0)
            milestones.useful_context()
            return result_for(current)

        return LiveContextVariantRunner(variant(name), execute, timeout_seconds=1)

    fixture = await ContextBenchmarkRuntime(
        snapshot=snap,
        cases=cases,
        runners=(runner("baseline"), runner("integrated")),
        repetitions=2,
    ).run()

    assert calls == [
        ("baseline", "case-a"), ("baseline", "case-a"),
        ("baseline", "case-b"), ("baseline", "case-b"),
        ("integrated", "case-a"), ("integrated", "case-a"),
        ("integrated", "case-b"), ("integrated", "case-b"),
    ]
    assert len(fixture.observations) == 8
    assert all(value.status == "completed" for value in fixture.observations)
    encoded = repr(fixture.observations)
    assert "sealed query" not in encoded
    assert all(value.scout_ms is not None for value in fixture.observations)
    assert all(value.first_useful_context_ms is not None
               for value in fixture.observations)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_timeout_failure_and_missing_milestone_are_bounded_unfinished_receipts():
    snap = snapshot()
    current = benchmark_case(snap)

    async def slow(_case, _milestones):
        await asyncio.sleep(1)

    async def failed(_case, _milestones):
        raise RuntimeError("private provider detail")

    async def omitted(case, _milestones):
        return result_for(case)

    fixture = await ContextBenchmarkRuntime(
        snapshot=snap,
        cases=(current,),
        runners=(
            LiveContextVariantRunner(variant("slow"), slow, 0.001),
            LiveContextVariantRunner(variant("failed"), failed, 1),
            LiveContextVariantRunner(variant("omitted"), omitted, 1),
        ),
        repetitions=1,
    ).run()

    outcomes = {value.variant_id: (value.status, value.error_code)
                for value in fixture.observations}
    assert outcomes == {
        "slow": ("timed_out", "variant_timeout"),
        "failed": ("failed", "variant_failed"),
        "omitted": ("failed", "variant_failed"),
    }
    assert "private provider detail" not in repr(fixture.observations)
    assert all(not value.discovered_sources for value in fixture.observations)


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_to_variant():
    snap = snapshot()
    cancelled = asyncio.Event()

    async def waiting(_case, milestones):
        milestones.scout_complete()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    runner = ContextBenchmarkRuntime(
        snapshot=snap,
        cases=(benchmark_case(snap),),
        runners=(
            LiveContextVariantRunner(variant("waiting"), waiting),
            LiveContextVariantRunner(variant("unused"), waiting),
        ),
        repetitions=1,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_runtime_rejects_drift_duplicates_and_unbounded_inputs():
    snap = snapshot()
    current = benchmark_case(snap)

    async def execute(case, milestones):
        milestones.scout_complete()
        return result_for(case)

    one = LiveContextVariantRunner(variant("one"), execute)
    two = LiveContextVariantRunner(variant("two"), execute)
    with pytest.raises(ValueError, match="crossed its dataset snapshot"):
        ContextBenchmarkRuntime(snapshot=snapshot("other"), cases=(current,),
                                runners=(one, two), repetitions=1)
    with pytest.raises(ValueError, match="case identities must be unique"):
        ContextBenchmarkRuntime(snapshot=snap, cases=(current, current),
                                runners=(one, two), repetitions=1)
    with pytest.raises(ValueError, match="variant identities must be unique"):
        ContextBenchmarkRuntime(snapshot=snap, cases=(current,),
                                runners=(one, one), repetitions=1)
    with pytest.raises(ValueError, match="repetitions"):
        ContextBenchmarkRuntime(snapshot=snap, cases=(current,),
                                runners=(one, two), repetitions=1001)
    with pytest.raises(ValueError, match="timeout"):
        LiveContextVariantRunner(variant("bad"), execute, float("nan"))


def test_milestones_are_ordered_single_use_and_closed():
    value = BenchmarkMilestones(0)
    with pytest.raises(RuntimeError, match="cannot precede"):
        value.useful_context()
    value.scout_complete()
    with pytest.raises(RuntimeError, match="already marked"):
        value.scout_complete()
    value.useful_context()
    with pytest.raises(RuntimeError, match="already marked"):
        value.useful_context()
    value.close()
    with pytest.raises(RuntimeError, match="closed"):
        value.scout_complete()


@pytest.mark.critical
@pytest.mark.asyncio
async def test_snapshot_adapter_binding_preserves_authority_and_requires_explicit_claim_support():
    snap = snapshot()
    current = benchmark_case(snap)

    class Adapter:
        profile = RetrievalProviderProfile("source-case-a", "fabric_vector", "r1")

        async def retrieve(self, supplied_snapshot, binding, cancellation):
            assert supplied_snapshot == snap
            assert binding.query_text == current.request.query
            cancellation.checkpoint()
            return binding.case.relevant_citations

    bound = snapshot_retrieval_runner(
        snapshot=snap,
        adapter=Adapter(),
        variant=variant("native"),
        source_id="sealed-corpus",
        claim_support=lambda case, citations: (
            case.required_claim_ids if citations else ()),
    )
    fallback = snapshot_retrieval_runner(
        snapshot=snap,
        adapter=Adapter(),
        variant=variant("no-claim-resolver"),
        source_id="sealed-corpus",
    )
    fixture = await ContextBenchmarkRuntime(
        snapshot=snap,
        cases=(current,),
        runners=(bound, fallback),
        repetitions=1,
    ).run()
    observations = {value.variant_id: value for value in fixture.observations}
    native = observations["native"]
    assert native.hits[0].authority == current.relevant_authorities[0]
    assert native.supported_claim_ids == current.required_claim_ids
    assert native.selected_sources == ("sealed-corpus",)
    assert observations["no-claim-resolver"].supported_claim_ids == ()
    assert current.request.query not in repr(fixture.observations)


def test_snapshot_binding_does_not_conflate_provider_and_source_identity():
    snap = snapshot()

    class Adapter:
        profile = RetrievalProviderProfile("provider-one", "fabric_vector", "r1")

        async def retrieve(self, *_args):
            return ()

    runner = snapshot_retrieval_runner(
        snapshot=snap, adapter=Adapter(), variant=variant("one"),
        source_id="corpus-one")
    assert runner.variant.variant_id == "one"
    with pytest.raises(ValueError, match="source ID"):
        snapshot_retrieval_runner(
            snapshot=snap, adapter=Adapter(), variant=variant("two"),
            source_id="not bounded")
