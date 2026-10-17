"""Opt-in runtime entry points for Vera's portable discovery route."""
from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone
from typing import Any

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:
    from vera.discovery_contract import DiscoveryRequest
    from vera.discovery_native_web import (
        CapabilityDiscoveryCollector, CapabilityDiscoveryScout,
        NativeWebTargetStore,
    )
    from vera.discovery_orchestration import DiscoveryRoutePolicy, run_discovery_route
    from vera.discovery_routing import DiscoveryWorkerOffer, WorkerProviderBinding
except ImportError:  # pragma: no cover - installed application spelling
    from Vera.vera.discovery_contract import DiscoveryRequest
    from Vera.vera.discovery_native_web import (
        CapabilityDiscoveryCollector, CapabilityDiscoveryScout,
        NativeWebTargetStore,
    )
    from Vera.vera.discovery_orchestration import DiscoveryRoutePolicy, run_discovery_route
    from Vera.vera.discovery_routing import DiscoveryWorkerOffer, WorkerProviderBinding


SEARCH_REVISION = "web.search-adapter/v1"
FETCH_REVISION = "web.fetch-adapter/v1"
WORKER_ID = "vera.local.discovery"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _call_registered(name: str, **kwargs: Any) -> Any:
    entry = CAPABILITY_REGISTRY.get(name) or {}
    fn = entry.get("raw") or entry.get("func")
    if fn is None:
        raise RuntimeError(f"{name.replace('.', '_')}_unavailable")
    result = fn(**kwargs)
    return await result if inspect.isawaitable(result) else result


def _report_dict(report) -> dict[str, Any]:
    return {
        "schema": "vera.discovery-runtime-report/v1",
        "mode": "opt_in",
        "authority": {
            "search": "web.search", "fetch": "web.fetch",
            "retry": "native", "persistence": "disabled",
            "consumer_routing_changed": False,
        },
        "result": report.result.to_dict(),
        "plan": {
            "plan_id": report.execution_plan.plan_id,
            "assignments": len(report.execution_plan.assignments),
            "refusals": len(report.execution_plan.refusals),
        },
        "ranking": [{
            "candidate_id": row.candidate_id, "source_id": row.source_id,
            "option_id": row.option_id, "score": row.score, "rank": row.rank,
        } for row in report.ranked_options],
        "failures": [{
            "stage": row.stage, "participant": row.participant,
            "reason": row.reason, "candidate_id": row.candidate_id,
            "option_id": row.option_id,
        } for row in report.failures],
        "rejected_option_ids": list(report.rejected_option_ids),
    }


@capability(
    "discovery.route.run", http_method="POST", http_path="/discovery/route/run",
    http_tags=["discovery", "context"], memory="off", silent=False,
    redact_args=["query"], redact_result=True,
    description="Run Vera's opt-in portable discovery route over the existing web.search and web.fetch "
                "capabilities. This does not replace any Chat, Research, Intel or Fabric path. It disables "
                "web.search discovery refresh and web.fetch Fabric ingestion, performs no retries, and uses "
                "one short-lived local CPU/network worker offer for this invocation. Input: query (str!), "
                "max_sources (1..16), max_context_items (1..32), timeout_ms (1000..60000), max_bytes "
                "(500..1000000). Output: portable result, plan/ranking/failure evidence and explicit native "
                "authority metadata.",
)
async def cap_discovery_route_run(query: str, max_sources: int = 6,
                                  max_context_items: int = 8,
                                  timeout_ms: int = 12_000,
                                  max_bytes: int = 200_000,
                                  trace_id=None):
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "query_required"}
    max_sources = max(1, min(16, int(max_sources)))
    max_context_items = max(1, min(32, int(max_context_items)))
    timeout_ms = max(1_000, min(60_000, int(timeout_ms)))
    max_bytes = max(500, min(1_000_000, int(max_bytes)))
    scout_ms = max(400, min(10_000, timeout_ms // 3))
    collection_ms = timeout_ms - scout_ms
    observed_ms = int(time.time() * 1_000)
    observed_at = _now()

    async def search(**kwargs):
        return await _call_registered("web.search", **kwargs, discover="off")

    async def fetch(**kwargs):
        return await _call_registered(
            "web.fetch", **kwargs, max_chars=max_bytes,
            ingest_to_fabric=False, await_ingest=False)

    targets = NativeWebTargetStore(maximum=max_sources)
    scout = CapabilityDiscoveryScout(
        search, targets, search_revision=SEARCH_REVISION,
        fetch_revision=FETCH_REVISION, clock=lambda: observed_at)
    collector = CapabilityDiscoveryCollector(
        fetch, targets, worker_id=WORKER_ID,
        provider_revision=FETCH_REVISION, clock=_now)
    worker = DiscoveryWorkerOffer(
        worker_id=WORKER_ID, evidence_source="discovery.runtime",
        observed_at_ms=observed_ms, valid_until_ms=observed_ms + min(timeout_ms, 60_000),
        resources=("network",), methods=("capability",),
        bindings=(WorkerProviderBinding("web.fetch", FETCH_REVISION),),
        concurrency_limit=1, in_flight=0)
    request = DiscoveryRequest(
        query=query, requester="capability.discovery.route.run",
        tenant_id="vera.local", namespace="discovery.opt_in", as_of=observed_at,
        source_kinds=("web",), max_sources=max_sources,
        max_context_items=max_context_items, timeout_ms=timeout_ms,
        max_bytes=max_bytes, max_cost_units=0)
    report = await run_discovery_route(
        request, (scout,), (collector,), (worker,), (),
        policy=DiscoveryRoutePolicy(
            max_parallel_scouts=1, max_parallel_collections=1,
            scout_timeout_ms=scout_ms, collection_timeout_ms=collection_ms),
        as_of_ms=observed_ms)
    return {"ok": True, **_report_dict(report)}
