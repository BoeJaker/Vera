"""Compose an exact discovery route through Vera's existing context registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from vera.context_provider import assemble_context
from vera.context_registry import (
    ContextComposition,
    ContextRegistry,
    DiscoveryContextSelection,
    DiscoveryContextSelectionPolicy,
)
from vera.discovery_contract import DiscoveryRequest
from vera.discovery_orchestration import (
    DiscoveryCancellation,
    DiscoveryCollector,
    DiscoveryRoutePolicy,
    DiscoveryRouteReport,
    DiscoveryScout,
    run_discovery_route,
)
from vera.discovery_routing import DiscoveryWorkerOffer, GpuAdmissionReceipt


@dataclass(frozen=True, slots=True)
class DiscoveryContextRouteReport:
    """One route, its manifest-bound selection, and resulting composition."""

    route: DiscoveryRouteReport
    selection: DiscoveryContextSelection
    composition: ContextComposition

    def __post_init__(self) -> None:
        if not isinstance(self.route, DiscoveryRouteReport):
            raise ValueError("context route requires a discovery route report")
        if not isinstance(self.selection, DiscoveryContextSelection):
            raise ValueError("context route requires a registry selection")
        if not isinstance(self.composition, ContextComposition):
            raise ValueError("context route requires a context composition")
        if self.selection.result_id != self.route.result.result_id:
            raise ValueError("context route selection belongs to another result")
        if self.composition.providers != self.selection.provider_ids:
            raise ValueError("context composition crossed its selected providers")
        if not set(self.composition.rankers).issubset(self.selection.ranker_ids):
            raise ValueError("context composition used an unselected ranker")


def _validate_composition_request(
        registry: ContextRegistry, ranker_ids: Sequence[str],
        selection_policy: DiscoveryContextSelectionPolicy,
        limit_per_provider: int, budget_tokens: int,
) -> tuple[tuple[str, ...], str]:
    if not isinstance(registry, ContextRegistry):
        raise ValueError("registry must be ContextRegistry")
    if not isinstance(selection_policy, DiscoveryContextSelectionPolicy):
        raise ValueError("selection_policy must be DiscoveryContextSelectionPolicy")
    if isinstance(limit_per_provider, bool) or not isinstance(limit_per_provider, int) \
            or limit_per_provider <= 0:
        raise ValueError("context provider limit must be positive")
    # Reuse the canonical budget validator before discovery can have effects.
    assemble_context((), budget_tokens=budget_tokens)
    if isinstance(ranker_ids, (str, bytes)):
        raise ValueError("context ranker selection must be a sequence")
    try:
        ranker_ids = tuple(ranker_ids)
    except TypeError as exc:
        raise ValueError("context ranker selection must be a sequence") from exc
    if (len(ranker_ids) != len(set(ranker_ids)) or
            any(not isinstance(value, str) or not value.strip() or
                value != value.strip() for value in ranker_ids)):
        raise ValueError("context ranker selection must be canonical and unique")
    available = {value.component_id for value in registry.manifest()
                 if value.role == "ranker"}
    unknown = sorted(set(ranker_ids) - available)
    if unknown:
        raise ValueError("unknown context ranker: " + ", ".join(unknown))
    return ranker_ids, registry.manifest_id()


async def run_discovery_context_route(
    request: DiscoveryRequest,
    scouts: Sequence[DiscoveryScout],
    collectors: Sequence[DiscoveryCollector],
    workers: Sequence[DiscoveryWorkerOffer],
    gpu_admissions: Sequence[GpuAdmissionReceipt],
    *,
    registry: ContextRegistry,
    limit_per_provider: int,
    budget_tokens: int,
    as_of_ms: int,
    ranker_ids: Sequence[str] = (),
    selection_policy: DiscoveryContextSelectionPolicy = DiscoveryContextSelectionPolicy(),
    route_policy: DiscoveryRoutePolicy = DiscoveryRoutePolicy(),
    cancellation: DiscoveryCancellation | None = None,
) -> DiscoveryContextRouteReport:
    """Run discovery, bind its authorities, then compose through one registry.

    Providers remain injected. The registry is never discovered implicitly and
    the query is passed in memory from the exact request rather than copied into
    a second request or report.
    """
    ranker_ids, registry_manifest_id = _validate_composition_request(
        registry, ranker_ids, selection_policy,
        limit_per_provider, budget_tokens)
    if cancellation is not None:
        cancellation.checkpoint()
    route = await run_discovery_route(
        request, scouts, collectors, workers, gpu_admissions,
        policy=route_policy, as_of_ms=as_of_ms, cancellation=cancellation)
    if cancellation is not None:
        cancellation.checkpoint()
    if registry.manifest_id() != registry_manifest_id:
        raise ValueError("context registry changed during discovery")
    selection = registry.select_discovery_result(
        route.result, ranker_ids=ranker_ids, policy=selection_policy)
    composition = await registry.compose_selection(
        selection, request.query, limit_per_provider=limit_per_provider,
        budget_tokens=budget_tokens, cancellation=cancellation)
    return DiscoveryContextRouteReport(route, selection, composition)
