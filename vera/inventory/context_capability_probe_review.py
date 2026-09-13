"""Source-bound review of Context's direct named-capability dependencies.

The review reads source declarations only. It does not import optional
subsystems, resolve a live capability, invoke a provider, or alter Context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence

SCHEMA = "vera.context-capability-probe-review/v1"
SOURCE = "vera/fabric/context.py"
MAX_SOURCE_BYTES = 2_000_000
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_KINDS = {"delegate", "preferred", "compatibility_fallback", "optional_augmentation"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextCapabilityProbe:
    capability: str
    semantic_role: str
    probe_kind: str
    required: bool
    ordered_after: str = ""
    subsystem: str = ""
    probe_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(str(self.capability or "")):
            raise ValueError("capability must be a bounded dotted identifier")
        for attr in ("semantic_role", "subsystem"):
            value = str(getattr(self, attr) or "").strip()
            if value and not _NAME.fullmatch(value):
                raise ValueError(f"{attr} must be a bounded identifier")
            object.__setattr__(self, attr, value)
        if self.probe_kind not in _KINDS:
            raise ValueError("unsupported Context probe kind")
        if self.ordered_after and not _NAME.fullmatch(self.ordered_after):
            raise ValueError("ordered_after must name a capability")
        if self.probe_kind == "compatibility_fallback" and not self.ordered_after:
            raise ValueError("compatibility fallback requires its preferred capability")
        object.__setattr__(self, "probe_id", _identity("ccprp_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability, "semantic_role": self.semantic_role,
            "probe_kind": self.probe_kind, "required": self.required,
            "ordered_after": self.ordered_after, "subsystem": self.subsystem,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"probe_id": self.probe_id, **self.identity_dict()}


CURRENT_PROBES = (
    ContextCapabilityProbe(
        "skills.active_context", "context.skills.render", "delegate", False,
        subsystem="skills"),
    ContextCapabilityProbe(
        "context.related_qa_block", "context.related_qa", "preferred", False,
        subsystem="memory"),
    ContextCapabilityProbe(
        "memory.recall_2nd_order", "context.related_qa",
        "compatibility_fallback", False,
        ordered_after="context.related_qa_block", subsystem="memory"),
    ContextCapabilityProbe(
        "worldview.query", "context.jepa_worldview.neighbours",
        "optional_augmentation", False, subsystem="jepa_worldview"),
    ContextCapabilityProbe(
        "worldview.rollout", "context.jepa_worldview.rollout",
        "optional_augmentation", False, subsystem="jepa_worldview"),
    ContextCapabilityProbe(
        "fabric.query", "context.fabric.retrieve", "optional_augmentation",
        False, subsystem="data_fabric"),
)

SOURCE_ASSERTIONS = (
    'CAPABILITY_REGISTRY.get("skills.active_context")',
    'CAPABILITY_REGISTRY.get("context.related_qa_block")',
    'CAPABILITY_REGISTRY.get("memory.recall_2nd_order")',
    'CAPABILITY_REGISTRY.get("worldview.query")',
    'CAPABILITY_REGISTRY.get("worldview.rollout")',
    'CAPABILITY_REGISTRY.get("fabric.query", {}).get("func")',
    '_BASE_DISCOVERY_CAPS = [', '_BASE_ESSENTIAL_CAPS = [',
)


def _source_evidence(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    path = (root / SOURCE).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"source is unavailable: {SOURCE}")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"source is too large: {SOURCE}")
    text = raw.decode("utf-8")
    missing = [item for item in SOURCE_ASSERTIONS if item not in text]
    if missing:
        raise ValueError(f"source assertions are missing from {SOURCE}: {missing}")
    return {
        "source_path_digest": "sha256:" + hashlib.sha256(SOURCE.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "assertion_digests": [
            "sha256:" + hashlib.sha256(item.encode()).hexdigest()
            for item in SOURCE_ASSERTIONS],
    }


def build_context_capability_probe_review(
    repo_root: Path,
    probes: Sequence[ContextCapabilityProbe] = CURRENT_PROBES,
) -> dict[str, Any]:
    """Classify Context's current probes without resolving or invoking them."""
    probes = tuple(sorted(probes, key=lambda item: item.probe_id))
    if not probes or len({item.probe_id for item in probes}) != len(probes):
        raise ValueError("Context probes must be non-empty and unique")
    capabilities = {item.capability for item in probes}
    for item in probes:
        if item.ordered_after and item.ordered_after not in capabilities:
            raise ValueError("fallback preference must reference a declared probe")
    roles: dict[str, list[str]] = {}
    for item in probes:
        roles.setdefault(item.semantic_role, []).append(item.capability)
    for values in roles.values():
        values.sort()
    payload = {
        "schema": SCHEMA, "probes": [item.to_dict() for item in probes],
        "source_evidence": _source_evidence(Path(repo_root)),
        "capabilities_by_semantic_role": dict(sorted(roles.items())),
        "bootstrap_policy": {
            "source": SOURCE, "discovery_list": "_BASE_DISCOVERY_CAPS",
            "essential_list": "_BASE_ESSENTIAL_CAPS",
            "classification": "static_loop_bootstrap_policy_not_dependency_resolution",
        },
        "worldview_scope": {
            "target": "jepa_worldview",
            "capabilities": ["worldview.query", "worldview.rollout"],
            "not_targeted": ["non_jepa_worldview", "godseye"],
            "dataset_interoperation": "separate_snapshot_and_evidence_boundary",
        },
        "findings": [
            "semantic_dependencies_are_encoded_as_registry_names",
            "related_qa_fallback_order_is_behavior",
            "optional_absence_is_currently_a_supported_state",
            "bootstrap_tool_lists_are_policy_not_a_generic_resolver",
            "worldview_probes_target_the_jepa_implementation"],
        "recommendations": [
            "introduce_a_typed_context_dependency_manifest",
            "resolve_by_semantic_role_with_explicit_ordered_compatibility_names",
            "retain_current_names_arguments_results_and_optional_absence_behavior",
            "keep_loop_bootstrap_policy_separate_from_dependency_resolution",
            "identify_jepa_worldview_explicitly_in_context_evidence",
            "do_not_alias_non_jepa_worldview_or_godseye_to_jepa_capabilities",
            "measure_stored_external_and_runtime_consumers_before_any_retirement"],
        "coverage": {
            "source_direct_probes": "complete_for_declared_context_source",
            "stored_consumers": "not_examined", "external_consumers": "not_examined",
            "runtime_calls": "not_examined", "live_optional_subsystems": "not_run"},
        "removal_authority": False, "changes_resolution": False,
        "changes_bootstrap_policy": False, "invokes_capabilities": False,
        "imports_optional_subsystems": False, "contacts_models": False,
        "mutates": False,
    }
    return {"review_id": _identity("ccpr_", payload), **payload}
