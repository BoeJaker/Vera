from pathlib import Path

import pytest

from vera.discovery_contract import CollectionOption, DiscoveryRequest, SourceCandidate
from vera.discovery_routing import (
    DiscoveryWorkerOffer,
    GpuAdmissionReceipt,
    WorkerProviderBinding,
    plan_discovery_execution,
)


NOW = "2026-09-28T09:00:00Z"
AS_OF = 1_800_000


def request(**changes):
    values = dict(query="find data", requester="agent.runner",
                  tenant_id="tenant.one", namespace="research.general",
                  as_of=NOW, source_kinds=("api",), max_sources=8,
                  max_context_items=16, timeout_ms=5000,
                  max_bytes=10_000, max_cost_units=10)
    values.update(changes)
    return DiscoveryRequest(**values)


def option(source="source.one", resource="cpu", method="api", **changes):
    values = dict(source_id=source, method=method, provider="web.adapter",
                  provider_revision="rev-1", resource=resource,
                  output_kinds=("context",), estimated_latency_ms=50,
                  estimated_cost_units=1, max_bytes=1000,
                  network_required=method in {"api", "crawl"})
    values.update(changes)
    return CollectionOption(**values)


def candidate(req, opt, relevance=.8):
    return SourceCandidate(
        request_id=req.request_id, source_id=opt.source_id,
        locator="https://example.test/data", source_kind="api",
        provider="discovery.scout", revision="index-1", observed_at=NOW,
        authority_score=.7, relevance_score=relevance, freshness_score=.9,
        options=(opt,))


def worker(worker_id="cpu-1", *, resources=("cpu", "network"),
           methods=("api", "crawl"), in_flight=0, concurrency=2,
           observed=AS_OF - 100, valid=AS_OF + 100):
    return DiscoveryWorkerOffer(
        worker_id=worker_id, evidence_source="worker.registry",
        observed_at_ms=observed, valid_until_ms=valid, resources=resources,
        methods=methods,
        bindings=(WorkerProviderBinding("web.adapter", "rev-1"),),
        concurrency_limit=concurrency, in_flight=in_flight)


def admission(req, opt, worker_id="gpu-1", state="granted", **changes):
    values = dict(request_id=req.request_id, option_id=opt.option_id,
                  worker_id=worker_id, gate_id="ollama.gpu.gate", state=state,
                  observed_at_ms=AS_OF - 10, valid_until_ms=AS_OF + 10,
                  lease_id="lease-1" if state == "granted" else "",
                  reason="" if state == "granted" else "gate_occupied",
                  holder="" if state == "granted" else "census")
    values.update(changes)
    return GpuAdmissionReceipt(**values)


@pytest.mark.critical
def test_cpu_plan_uses_current_exact_worker_offer_without_execution():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (worker(),), (), as_of_ms=AS_OF)
    assert len(plan.assignments) == 1
    assert plan.assignments[0].worker_id == "cpu-1"
    assert plan.assignments[0].worker_evidence_id.startswith("dwo_")
    assert not plan.assignments[0].gpu_admission_id
    assert not plan.refusals


@pytest.mark.critical
def test_gpu_never_routes_from_health_without_exact_current_gate_admission():
    req = request()
    opt = option(resource="gpu")
    cand = candidate(req, opt)
    gpu = worker("gpu-1", resources=("gpu", "network"))
    missing = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (gpu,), (), as_of_ms=AS_OF)
    assert missing.refusals[0].reason == "gpu_admission_missing_or_stale"

    stale = admission(req, opt, observed_at_ms=AS_OF - 100,
                      valid_until_ms=AS_OF - 1)
    stale_plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (gpu,), (stale,), as_of_ms=AS_OF)
    assert stale_plan.refusals[0].reason == "gpu_admission_missing_or_stale"


@pytest.mark.critical
def test_census_held_gate_is_an_explicit_denial_not_free_capacity():
    req = request()
    opt = option(resource="gpu")
    cand = candidate(req, opt)
    gpu = worker("gpu-1", resources=("gpu", "network"))
    denied = admission(req, opt, state="denied")
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (gpu,), (denied,), as_of_ms=AS_OF)
    assert not plan.assignments
    assert plan.refusals[0].reason == "gpu_gate_denied"
    assert plan.refusals[0].evidence_id == denied.admission_id


def test_current_gpu_admission_routes_exact_option_and_worker_only():
    req = request()
    opt = option(resource="gpu")
    cand = candidate(req, opt)
    gpu = worker("gpu-1", resources=("gpu", "network"))
    granted = admission(req, opt)
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (gpu,), (granted,), as_of_ms=AS_OF)
    assert plan.assignments[0].gpu_admission_id == granted.admission_id

    other = admission(request(query="other"), opt)
    wrong = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (gpu,), (other,), as_of_ms=AS_OF)
    assert wrong.refusals[0].reason == "gpu_admission_missing_or_stale"


def test_stale_worker_snapshot_is_never_capacity_evidence():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    stale = worker(observed=AS_OF - 200, valid=AS_OF - 1)
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (stale,), (), as_of_ms=AS_OF)
    assert plan.refusals[0].reason == "no_current_worker_offer"


def test_worker_must_match_resource_method_and_provider_revision():
    req = request()
    opt = option()
    cand = candidate(req, opt)
    wrong_method = worker(methods=("crawl",))
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (wrong_method,), (), as_of_ms=AS_OF)
    assert plan.refusals[0].reason == "no_current_worker_offer"

    no_network = worker(resources=("cpu",))
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (no_network,), (), as_of_ms=AS_OF)
    assert plan.refusals[0].reason == "no_current_worker_offer"

    wrong_binding = DiscoveryWorkerOffer(
        worker_id="cpu-1", evidence_source="worker.registry",
        observed_at_ms=AS_OF - 1, valid_until_ms=AS_OF + 1,
        resources=("cpu",), methods=("api",),
        bindings=(WorkerProviderBinding("web.adapter", "rev-2"),),
        concurrency_limit=1, in_flight=0)
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (wrong_binding,), (), as_of_ms=AS_OF)
    assert plan.refusals[0].reason == "no_current_worker_offer"


def test_capacity_is_reserved_deterministically_by_relevance_then_load():
    req = request()
    first = option("source.one")
    second = option("source.two")
    candidates = (candidate(req, first, .9), candidate(req, second, .8))
    one_slot = worker(concurrency=1)
    plan = plan_discovery_execution(
        req, candidates, (second.option_id, first.option_id), (one_slot,), (),
        as_of_ms=AS_OF)
    assert [value.option_id for value in plan.assignments] == [first.option_id]
    assert plan.refusals[0].reason == "no_current_worker_offer"


def test_one_source_cannot_select_two_collection_methods_in_one_plan():
    req = request()
    first = option(method="api")
    second = option(method="crawl")
    cand = SourceCandidate(
        request_id=req.request_id, source_id="source.one",
        locator="https://example.test/data", source_kind="api",
        provider="discovery.scout", revision="index-1", observed_at=NOW,
        authority_score=.7, relevance_score=.8, freshness_score=.9,
        options=(first, second))
    with pytest.raises(ValueError, match="one collection option"):
        plan_discovery_execution(
            req, (cand,), (first.option_id, second.option_id), (worker(),), (),
            as_of_ms=AS_OF)


@pytest.mark.parametrize(("request_change", "option_change", "reason"), [
    ({"max_cost_units": 0}, {"estimated_cost_units": 1}, "cost_budget_exceeded"),
    ({"max_bytes": 999}, {"max_bytes": 1000}, "byte_budget_exceeded"),
])
def test_plan_refuses_selected_work_that_exceeds_budget(request_change,
                                                        option_change, reason):
    req = request(**request_change)
    opt = option(**option_change)
    cand = candidate(req, opt)
    plan = plan_discovery_execution(
        req, (cand,), (opt.option_id,), (worker(),), (), as_of_ms=AS_OF)
    assert plan.refusals[0].reason == reason


def test_worker_and_admission_validity_are_strictly_bounded():
    with pytest.raises(ValueError, match="at most 60 seconds"):
        worker(observed=0, valid=60_001)
    req = request()
    opt = option(resource="gpu")
    with pytest.raises(ValueError, match="at most 60 seconds"):
        admission(req, opt, observed_at_ms=0, valid_until_ms=60_001)
    with pytest.raises(ValueError, match="requires a reason"):
        admission(req, opt, state="denied", reason="")


def test_router_module_has_no_runtime_gate_clock_or_dispatch_dependency():
    source = (Path(__file__).parents[1] / "vera" / "discovery_routing.py").read_text()
    for forbidden in ("time.time", "datetime.now", "CAPABILITY_REGISTRY",
                      "dispatch_task", "ollama_gate", "requests", "httpx"):
        assert forbidden not in source
