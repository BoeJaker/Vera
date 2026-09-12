"""Source-bound review of repeated agent-bridge lifecycle plumbing.

This module inventories declarations only. It does not inspect Docker, build an
image, import an optional agent framework, contact a model, or launch a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

SCHEMA = "vera.bridge-runtime-boilerplate-review/v1"
MAX_SOURCE_BYTES = 2_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BridgeLifecycleSurface:
    runtime_id: str
    status_path: str
    image_path: str
    run_path: str
    integration_level: str
    adapter_registered: bool
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("runtime_id", "integration_level"):
            value = str(getattr(self, attr) or "").strip()
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{attr} must be a bounded identifier")
            object.__setattr__(self, attr, value)
        if self.integration_level not in {"adapter_facade", "shared_runner_only"}:
            raise ValueError("unsupported bridge integration level")
        paths = (self.status_path, self.image_path, self.run_path)
        if any(not path.startswith("vera/") or ".." in path.split("/") for path in paths):
            raise ValueError("bridge source paths must stay within vera")
        object.__setattr__(self, "surface_id", _identity("brls_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "status_path": self.status_path,
            "image_path": self.image_path,
            "run_path": self.run_path,
            "integration_level": self.integration_level,
            "adapter_registered": self.adapter_registered,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    BridgeLifecycleSurface(
        "langgraph", "vera/langgraph/langgraph_capabilities.py",
        "vera/langgraph/langgraph_capabilities.py",
        "vera/langgraph/langgraph_capabilities.py", "adapter_facade", True),
    BridgeLifecycleSurface(
        "pydanticai", "vera/pydanticai/pydanticai_capabilities.py",
        "vera/pydanticai/pydanticai_capabilities.py",
        "vera/pydanticai/pydanticai_capabilities.py", "shared_runner_only", False),
    BridgeLifecycleSurface(
        "smolagents", "vera/smolagents/smolagents_capabilities.py",
        "vera/smolagents/smolagents_capabilities.py",
        "vera/smolagents/smolagents_capabilities.py", "shared_runner_only", False),
)

SOURCE_ASSERTIONS: Mapping[str, tuple[str, ...]] = {
    "vera/agentbridges/runtime_adapter.py": (
        "class RuntimeAdapter(Protocol):", "async def health(self)",
        "async def ensure_image(self", "async def run(self, request: ContainerRunRequest",
        "async def cancel(self, run_id: str)"),
    "vera/agentbridges/runtime_registry.py": ('"langgraph": ContainerRuntimeAdapter',),
    "vera/agentbridges/agentbridge_capabilities.py": (
        "present = await image_present(b.image)", 'cap_name = f"{bridge}.image.ensure"'),
    "vera/langgraph/langgraph_capabilities.py": (
        "health = await _ADAPTER.health()", "await _ADAPTER.image_present()",
        "_ADAPTER.ensure_image(", "_ADAPTER.run(request, emit=emit_event)"),
    "vera/pydanticai/pydanticai_capabilities.py": (
        "present = await image_present(_IMAGE) if docker_ok else False",
        "r = await build_image(_IMAGE", "asyncio.ensure_future(stream_bridge_container("),
    "vera/smolagents/smolagents_capabilities.py": (
        "present = await image_present(_IMAGE) if docker_ok else False",
        "r = await build_image(_IMAGE", "asyncio.ensure_future(stream_bridge_container("),
}


def _source_evidence(repo_root: Path, relative: str,
                     assertions: Sequence[str]) -> dict[str, Any]:
    root = repo_root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"source is unavailable: {relative}")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"source is too large: {relative}")
    text = raw.decode("utf-8")
    missing = [item for item in assertions if item not in text]
    if missing:
        raise ValueError(f"source assertions are missing from {relative}: {missing}")
    return {
        "source_path_digest": "sha256:" + hashlib.sha256(relative.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "assertion_digests": [
            "sha256:" + hashlib.sha256(item.encode()).hexdigest()
            for item in assertions],
    }


def build_bridge_runtime_boilerplate_review(
    repo_root: Path,
    surfaces: Sequence[BridgeLifecycleSurface] = CURRENT_SURFACES,
    source_assertions: Mapping[str, Sequence[str]] = SOURCE_ASSERTIONS,
) -> dict[str, Any]:
    """Classify the shared seam without executing any bridge lifecycle action."""
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface_id for item in surfaces}) != len(surfaces):
        raise ValueError("bridge lifecycle surfaces must be non-empty and unique")
    evidence = {
        source: _source_evidence(Path(repo_root), source, assertions)
        for source, assertions in sorted(source_assertions.items())}
    levels = {
        level: sorted(item.runtime_id for item in surfaces
                      if item.integration_level == level)
        for level in ("adapter_facade", "shared_runner_only")}
    payload = {
        "schema": SCHEMA,
        "surfaces": [item.to_dict() for item in surfaces],
        "source_evidence": evidence,
        "runtimes_by_integration_level": levels,
        "shared_authority": {
            "lifecycle_contract": "RuntimeAdapter",
            "container_execution": "stream_bridge_container",
            "run_request": "ContainerRunRequest",
            "active_run_ownership": "agentbridge_runtime",
        },
        "retained_runtime_specific_fields": [
            "capability_names", "event_prefix", "enabled_setting", "image",
            "dockerfile", "argv", "progress_kinds", "runtime_error_copy"],
        "findings": [
            "langgraph_uses_the_full_runtime_adapter_facade",
            "pydanticai_and_smolagents_share_the_low_level_runner_only",
            "status_and_image_build_wrappers_are_repeated",
            "run_validation_and_request_assembly_are_repeated",
            "catalog_image_health_bypasses_the_adapter_registry",
            "per_bridge_capability_and_event_identities_are_compatibility_surfaces"],
        "recommendations": [
            "add_static_adapter_descriptors_for_shipped_container_bridges",
            "delegate_common_health_image_and_run_lifecycle_to_runtime_adapter",
            "retain_per_bridge_capability_names_and_event_prefixes",
            "retain_provider_specific_argv_progress_and_error_semantics",
            "project_catalog_health_through_registered_adapters_with_explicit_fallback",
            "prove_success_error_timeout_cancel_teardown_and_gate_parity_before_migration",
            "measure_stored_external_and_runtime_consumers_before_any_retirement"],
        "coverage": {
            "source_lifecycle_surfaces": "complete_for_declared_runtimes",
            "stored_consumers": "not_examined", "external_consumers": "not_examined",
            "live_bridge_runs": "not_run", "image_builds": "not_run"},
        "removal_authority": False, "changes_registration": False,
        "changes_execution": False, "inspects_docker": False,
        "builds_images": False, "launches_bridges": False,
        "contacts_models": False, "mutates": False,
    }
    return {"review_id": _identity("brbr_", payload), **payload}
