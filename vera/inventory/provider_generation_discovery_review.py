"""Evidence-only review of generation entry points and loop discovery policy."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence


SCHEMA = "vera.provider-generation-discovery-review/v1"
ROLES = frozenset({"task_provider", "generation_broker", "backend_direct", "external_direct"})
DISCOVERY_CLASSES = frozenset({"task_facing_default", "raw_family_excluded", "direct_provider_unclassified"})
CONTRACT_STATUSES = frozenset({"declared", "missing"})
MAX_SOURCE_BYTES = 4 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _bounded(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class GenerationSurface:
    name: str
    role: str
    discovery_class: str
    task_family: str
    contract_status: str
    availability_scope: str
    source_path: str
    symbol: str
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("name", "task_family", "availability_scope"):
            object.__setattr__(self, attr, _bounded(getattr(self, attr), attr))
        if self.role not in ROLES:
            raise ValueError("unsupported generation role")
        if self.discovery_class not in DISCOVERY_CLASSES:
            raise ValueError("unsupported discovery class")
        if self.contract_status not in CONTRACT_STATUSES:
            raise ValueError("unsupported contract status")
        path = str(self.source_path or "").replace("\\", "/").strip("/")
        if not path.startswith("vera/") or ".." in path.split("/"):
            raise ValueError("source path must stay within vera")
        object.__setattr__(self, "source_path", path)
        if not _SYMBOL.fullmatch(str(self.symbol or "")):
            raise ValueError("source symbol must be a Python identifier")
        object.__setattr__(self, "surface_id", _identity("pgs_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "role": self.role,
            "discovery_class": self.discovery_class,
            "task_family": self.task_family,
            "contract_status": self.contract_status,
            "availability_scope": self.availability_scope,
            "source_path": self.source_path, "symbol": self.symbol,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    GenerationSurface("code.author", "task_provider", "task_facing_default",
                      "source_file.author", "declared", "core", "vera/dag/dag_workshop_capabilities.py",
                      "cap_code_author"),
    GenerationSurface("prose.author", "task_provider", "task_facing_default",
                      "document.author", "declared", "core", "vera/dag/dag_workshop_capabilities.py",
                      "cap_prose_author"),
    GenerationSurface("llm.generate", "generation_broker", "raw_family_excluded",
                      "text.generate", "declared", "core", "vera/capabilities/capabilities.py",
                      "llm_generate"),
    GenerationSurface("ollama.generate_raw", "backend_direct", "direct_provider_unclassified",
                      "text.generate", "declared", "core", "vera/capabilities/capabilities.py",
                      "ollama_generate_raw"),
    GenerationSurface("vllm.generate", "backend_direct", "direct_provider_unclassified",
                      "text.generate", "missing", "core", "vera/vllm/vllm_capabilities.py",
                      "cap_vllm_generate"),
    GenerationSurface("vllm.chat", "backend_direct", "direct_provider_unclassified",
                      "chat.complete", "missing", "core", "vera/vllm/vllm_capabilities.py",
                      "cap_vllm_chat"),
    GenerationSurface("providers.chat", "external_direct", "direct_provider_unclassified",
                      "chat.complete", "missing", "configured_external", "vera/providers/providers_capabilities.py",
                      "cap_providers_chat"),
)


POLICY_SOURCE = "vera/dag/dag_workshop_capabilities.py"
POLICY_ASSERTIONS = (
    '_UNIVERSAL_ESSENTIALS = ["code.author", "prose.author"]',
    'c.startswith("llm.")',
    "_loop_llm_caps_blocked()",
)
LOADER_SOURCE = "vera/capability_orchestration.py"
LOADER_ASSERTIONS = ('os.path.join(_here, "vllm/vllm_capabilities.py")',)


def _read_bound(root: Path, relative: str) -> tuple[bytes, str]:
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"source is unavailable: {relative}")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"source is too large: {relative}")
    return raw, raw.decode("utf-8")


def _surface_evidence(root: Path, surface: GenerationSurface) -> dict[str, Any]:
    raw, text = _read_bound(root, surface.source_path)
    if not re.search(rf"\b(?:async\s+def|def)\s+{re.escape(surface.symbol)}\s*\(", text):
        raise ValueError(f"source symbol is missing: {surface.symbol}")
    if f'"{surface.name}"' not in text:
        raise ValueError(f"capability declaration is missing: {surface.name}")
    return {
        "surface_id": surface.surface_id,
        "source_path_digest": "sha256:" + hashlib.sha256(surface.source_path.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _assertion_evidence(root: Path, source: str,
                        assertions: Sequence[str]) -> dict[str, Any]:
    raw, text = _read_bound(root, source)
    missing = [item for item in assertions if item not in text]
    if missing:
        raise ValueError(f"source assertions are missing from {source}: {missing}")
    return {
        "source_path_digest": "sha256:" + hashlib.sha256(source.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "assertion_digests": ["sha256:" + hashlib.sha256(item.encode()).hexdigest()
                              for item in assertions],
    }


def build_provider_generation_discovery_review(
    repo_root: Path, surfaces: Sequence[GenerationSurface] = CURRENT_SURFACES,
) -> dict[str, Any]:
    """Describe current discovery classification without invoking a provider."""
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface_id for item in surfaces}) != len(surfaces):
        raise ValueError("generation surfaces must be non-empty and unique")
    evidence = [_surface_evidence(Path(repo_root), item) for item in surfaces]
    by_class = {name: sorted(item.name for item in surfaces
                             if item.discovery_class == name)
                for name in sorted(DISCOVERY_CLASSES)}
    payload = {
        "schema": SCHEMA,
        "surfaces": [item.to_dict() for item in surfaces],
        "source_evidence": sorted(evidence, key=lambda item: item["surface_id"]),
        "default_discovery_policy_evidence": _assertion_evidence(
            Path(repo_root), POLICY_SOURCE, POLICY_ASSERTIONS),
        "default_loader_evidence": _assertion_evidence(
            Path(repo_root), LOADER_SOURCE, LOADER_ASSERTIONS),
        "surfaces_by_discovery_class": by_class,
        "missing_task_contracts": sorted(item.name for item in surfaces
                                         if item.contract_status == "missing"),
        "conclusion": "classify_direct_provider_entry_points_before_default_discovery_change",
        "recommendations": [
            "retain_all_provider_entry_points_as_explicitly_callable",
            "retain_task_facing_authors_as_default_loop_generation_tools",
            "retain_raw_llm_family_default_exclusion_unless_explicitly_enabled",
            "add_one_shared_discovery_policy_for_direct_provider_entry_points",
            "declare_task_and_effect_contracts_before_resolver_admission",
            "prefer_canonical_task_resolution_over_provider_name_selection",
            "measure_provider_consumer_and_runtime_evidence_before_policy_change",
        ],
        "coverage": {
            "source_and_default_policy": "complete_for_declared_surfaces",
            "stored_definitions": "not_examined",
            "external_consumers": "not_examined",
            "runtime_calls": "not_examined",
        },
        "removal_authority": False,
        "changes_discovery": False,
        "executes": False,
        "mutates": False,
    }
    return {"review_id": _identity("pgdr_", payload), **payload}
