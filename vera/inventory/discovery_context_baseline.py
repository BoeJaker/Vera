"""Offline inventory for Vera's discovery and context retrieval paths.

This module is deliberately source-bound. It does not import the inventoried
subsystems, resolve capabilities, contact a model, or probe a data source. Its
job is to make the current route surface reviewable before a shared discovery
contract changes it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Sequence


SCHEMA = "vera.discovery-context-baseline/v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ROLES = {"discover", "retrieve", "context", "data", "enrich", "route"}
_BOUNDARIES = {
    "capability", "context_provider", "dataset_provider", "query_provider",
    "artifact_provider", "memory_provider", "evidence_provider",
    "model_package", "native_adapter", "discovery_contract", "internal",
}
_EXECUTION = {"inline", "async_local", "remote_http", "provider_injected"}
_RESOURCES = {"cpu", "cpu_or_gpu_gated", "storage", "network", "none"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DiscoveryContextComponent:
    component_id: str
    role: str
    source: str
    source_assertion: str
    boundary: str
    execution: str
    resource: str
    data_contract: str
    enriches: tuple[str, ...] = ()
    live_required: bool = False

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.component_id):
            raise ValueError("component_id must be a bounded identifier")
        if self.role not in _ROLES:
            raise ValueError("unsupported component role")
        if self.boundary not in _BOUNDARIES:
            raise ValueError("unsupported component boundary")
        if self.execution not in _EXECUTION:
            raise ValueError("unsupported execution mode")
        if self.resource not in _RESOURCES:
            raise ValueError("unsupported resource class")
        path = PurePosixPath(self.source)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("source must be a repository-relative Python path")
        if not self.source_assertion or len(self.source_assertion) > 256:
            raise ValueError("source_assertion must be present and bounded")
        if not _IDENTIFIER.fullmatch(self.data_contract):
            raise ValueError("data_contract must be a bounded identifier")
        if any(not _IDENTIFIER.fullmatch(value) for value in self.enriches):
            raise ValueError("enriches entries must be bounded identifiers")
        if len(set(self.enriches)) != len(self.enriches):
            raise ValueError("enriches entries must be unique")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "role": self.role,
            "source": self.source,
            "boundary": self.boundary,
            "execution": self.execution,
            "resource": self.resource,
            "data_contract": self.data_contract,
            "enriches": list(self.enriches),
            "live_required": self.live_required,
        }


CURRENT_COMPONENTS = (
    DiscoveryContextComponent(
        "discovery.portable", "discover", "vera/discovery_contract.py",
        "class DiscoveryRequest", "discovery_contract", "provider_injected",
        "none", "vera.discovery-result.v1", ("context.portable", "fabric.dataset")),
    DiscoveryContextComponent(
        "discovery.routing", "route", "vera/discovery_routing.py",
        "def plan_discovery_execution", "discovery_contract", "provider_injected",
        "none", "vera.discovery-execution-plan.v1", ("discovery.portable",)),
    DiscoveryContextComponent(
        "discovery.orchestration", "route", "vera/discovery_orchestration.py",
        "async def run_discovery_route", "discovery_contract", "async_local",
        "cpu", "vera.discovery-route-report.v1",
        ("discovery.portable", "discovery.routing")),
    DiscoveryContextComponent(
        "discovery.benchmark", "data", "vera/discovery_benchmark.py",
        "def compare_context_benchmark", "discovery_contract", "inline",
        "none", "vera.discovery-context-benchmark.v1",
        ("discovery.orchestration", "context.enrichment-ledger")),
    DiscoveryContextComponent(
        "context.portable", "context", "vera/context_provider.py",
        "class ContextProvider(Protocol)", "context_provider", "provider_injected",
        "cpu", "vera.context-item.v1"),
    DiscoveryContextComponent(
        "context.registry", "route", "vera/context_registry.py",
        "def select_discovery_result", "discovery_contract", "async_local",
        "cpu", "vera.context-component-manifest.v1",
        ("context.portable", "discovery.portable")),
    DiscoveryContextComponent(
        "context.enrichment-ledger", "enrich", "vera/context_enrichment.py",
        "class ContextEnrichmentLedger", "context_provider", "inline", "none",
        "vera.context-enrichment-ledger.v1", ("context.portable",)),
    DiscoveryContextComponent(
        "fabric.discovery", "discover", "vera/fabric/discovery.py",
        '"fabric.discover.query"', "capability", "async_local", "network",
        "vera.fabric-discovery-native.v1", ("fabric.dataset", "fabric.memory"),
        True),
    DiscoveryContextComponent(
        "fabric.dataset", "data", "vera/fabric/dataset_provider.py",
        "class DatasetProvider(Protocol)", "dataset_provider", "provider_injected",
        "storage", "vera.dataset-snapshot.v1"),
    DiscoveryContextComponent(
        "fabric.query", "retrieve", "vera/fabric/dataset_provider.py",
        "class QueryProvider(Protocol)", "query_provider", "provider_injected",
        "storage", "vera.query-page.v1", ("context.portable",)),
    DiscoveryContextComponent(
        "fabric.artifact", "data", "vera/fabric/artifact_provider.py",
        "class ArtifactProvider(Protocol)", "artifact_provider", "provider_injected",
        "storage", "vera.artifact-stat.v1"),
    DiscoveryContextComponent(
        "fabric.memory", "retrieve", "vera/fabric/memory_provider.py",
        "class MemoryProvider(Protocol)", "memory_provider", "provider_injected",
        "storage", "vera.memory-page.v1", ("context.portable",)),
    DiscoveryContextComponent(
        "fabric.memory-context", "context",
        "vera/fabric/memory_context_provider.py", "class MemoryContextProvider",
        "context_provider", "provider_injected", "cpu", "vera.context-item.v1",
        ("context.portable",)),
    DiscoveryContextComponent(
        "fabric.native-vector", "retrieve", "vera/fabric/native_retrieval.py",
        "class NativeFabricVectorRetrievalAdapter", "native_adapter",
        "provider_injected", "cpu", "vera.retrieval-result.v1",
        ("fabric.query",)),
    DiscoveryContextComponent(
        "fabric.external-snapshot", "retrieve",
        "vera/fabric/external_retrieval.py",
        "class ExternalSnapshotRetrievalAdapter", "native_adapter",
        "provider_injected", "network", "vera.retrieval-result.v1",
        ("fabric.query",), True),
    DiscoveryContextComponent(
        "fabric.analytical", "retrieve", "vera/fabric/analytical_retrieval.py",
        "class AnalyticalSnapshotRetrievalAdapter", "native_adapter",
        "provider_injected", "cpu", "vera.retrieval-result.v1",
        ("fabric.query",)),
    DiscoveryContextComponent(
        "worldview.jepa-evidence", "enrich",
        "vera/worldview/evidence_provider.py", "class EvidenceProvider(Protocol)",
        "evidence_provider", "provider_injected", "cpu_or_gpu_gated",
        "vera.worldview-evidence.v1", ("context.portable",), True),
    DiscoveryContextComponent(
        "worldview.jepa-ranker", "enrich", "vera/worldview/context_ranker.py",
        "class WorldviewContextRanker", "context_provider", "provider_injected",
        "cpu", "vera.context-ranking-evidence.v1", ("context.portable",)),
    DiscoveryContextComponent(
        "worldview.jepa-model", "enrich", "vera/worldview/worldview_jepa.py",
        "class WorldView:", "native_adapter", "async_local",
        "cpu_or_gpu_gated", "vera.jepa-worldview-native.v1", (), True),
    DiscoveryContextComponent(
        "worldview.jepa-projection", "data",
        "vera/worldview/worldview_projection_adapter.py",
        "class WorldviewProjectionAdapter", "native_adapter", "provider_injected",
        "storage", "vera.worldview-projection.v1", ("worldview.jepa-evidence",)),
    DiscoveryContextComponent(
        "godseye.geospatial", "data", "vera/godseye/godseye_core.py",
        "def repo_root", "native_adapter", "async_local", "storage",
        "vera.godseye-native.v1", (), True),
    DiscoveryContextComponent(
        "godseye.portable-dataset", "data",
        "vera/godseye/portable_dataset.py", "def make_portable_dataset",
        "dataset_provider", "inline", "none",
        "vera.godseye-portable-dataset.v1", ("fabric.dataset",)),
    DiscoveryContextComponent(
        "nlp.dispatch", "enrich", "vera/research/nlp_dispatch_core.py",
        "def resolve_placement", "native_adapter", "remote_http", "cpu",
        "vera.nlp.text.v1", ("fabric.dataset",), True),
    DiscoveryContextComponent(
        "nlp.model-inventory", "data", "vera/models/nlp_inventory.py",
        "def project_nlp_inventory", "model_package", "provider_injected",
        "none", "vera.model-package.v1", ("nlp.dispatch",)),
    DiscoveryContextComponent(
        "agent.rag", "retrieve", "vera/agents/agents.py",
        "async def agent_rag_retrieve", "capability", "async_local", "storage",
        "vera.agent-rag-native.v1", ("context.portable",)),
    DiscoveryContextComponent(
        "agent.rag-context", "context", "vera/agents/rag_context_adapter.py",
        "def project_agent_rag_results", "context_provider", "inline", "none",
        "vera.context-item.v1", ("agent.rag", "context.portable")),
    DiscoveryContextComponent(
        "workers.node-choice", "route", "vera/workers/node_choice.py",
        "def choose", "internal", "inline", "none",
        "vera.worker-choice-native.v1"),
)


CURRENT_GAPS = (
    "discovery_does_not_share_context_registry_selection",
    "live_source_and_accelerator_evidence_is_not_part_of_this_offline_baseline",
)


def semantic_baseline(
    components: Sequence[DiscoveryContextComponent] = CURRENT_COMPONENTS,
) -> dict[str, Any]:
    """Return the stable, reviewable part of the current baseline."""
    ordered = tuple(sorted(components, key=lambda value: value.component_id))
    ids = [value.component_id for value in ordered]
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("components must be non-empty and uniquely identified")
    known = set(ids)
    for value in ordered:
        unknown = set(value.enriches) - known
        if unknown:
            raise ValueError(
                f"{value.component_id} enriches unknown components: {sorted(unknown)}")
    payload = {
        "schema": SCHEMA,
        "components": [value.semantic_dict() for value in ordered],
        "gaps": list(CURRENT_GAPS),
    }
    return {"baseline_id": _identity("dcb_", payload), **payload}


def _source_evidence(repo_root: Path,
                     components: Sequence[DiscoveryContextComponent]
                     ) -> list[dict[str, Any]]:
    root = repo_root.resolve()
    evidence = []
    for component in sorted(components, key=lambda value: value.component_id):
        path = (root / component.source).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"source is unavailable: {component.source}")
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise ValueError(f"source is too large: {component.source}")
        text = raw.decode("utf-8")
        if component.source_assertion not in text:
            raise ValueError(
                f"source assertion missing for {component.component_id}")
        evidence.append({
            "component_id": component.component_id,
            "source_path_digest": "sha256:" + hashlib.sha256(
                component.source.encode()).hexdigest(),
            "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digest": "sha256:" + hashlib.sha256(
                component.source_assertion.encode()).hexdigest(),
        })
    return evidence


def build_discovery_context_baseline(
    repo_root: Path,
    components: Sequence[DiscoveryContextComponent] = CURRENT_COMPONENTS,
) -> dict[str, Any]:
    """Build the offline baseline with current source evidence."""
    stable = semantic_baseline(components)
    evidence = _source_evidence(Path(repo_root), components)
    roles: dict[str, list[str]] = {}
    boundaries: dict[str, list[str]] = {}
    for component in sorted(components, key=lambda value: value.component_id):
        roles.setdefault(component.role, []).append(component.component_id)
        boundaries.setdefault(component.boundary, []).append(component.component_id)
    return {
        **stable,
        "inventory_id": _identity(
            "dci_", {"baseline_id": stable["baseline_id"],
                     "source_evidence": evidence}),
        "source_evidence": evidence,
        "components_by_role": dict(sorted(roles.items())),
        "components_by_boundary": dict(sorted(boundaries.items())),
        "counts": {
            "components": len(components),
            "live_evidence_required": sum(value.live_required for value in components),
            "gaps": len(CURRENT_GAPS),
        },
        "constraints": {
            "invokes_capabilities": False,
            "imports_inventoried_subsystems": False,
            "contacts_sources": False,
            "contacts_models": False,
            "uses_accelerators": False,
            "mutates": False,
        },
    }
