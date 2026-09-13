"""Source-bound review of ONNX artifact and capability identity authority.

The review reads repository source only.  It never imports ONNX Runtime,
opens a model artifact, registers a capability, or changes invocation policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SCHEMA = "vera.onnx-capability-authority-review/v1"
MAX_SOURCE_BYTES = 2_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.<>/-]{1,160}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _bounded(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class ONNXIdentitySurface:
    name: str
    identity_kind: str
    selector_kind: str
    authority: str
    callable_now: bool
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("name", "identity_kind", "selector_kind", "authority"):
            object.__setattr__(self, attr, _bounded(getattr(self, attr), attr))
        if self.identity_kind not in {
            "stable_executor", "artifact_bound_compatibility", "artifact_record"
        }:
            raise ValueError("unsupported ONNX identity kind")
        object.__setattr__(self, "surface_id", _identity("ocis_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "identity_kind": self.identity_kind,
            "selector_kind": self.selector_kind,
            "authority": self.authority,
            "callable_now": self.callable_now,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    ONNXIdentitySurface(
        "ml.onnx.run", "stable_executor", "artifact-argument",
        "capability-registry", True,
    ),
    ONNXIdentitySurface(
        "ml.onnx.model.<slug>", "artifact_bound_compatibility", "bound-slug",
        "onnx-manifest", True,
    ),
    ONNXIdentitySurface(
        "ModelPackage", "artifact_record", "package-id",
        "model-package-registry", False,
    ),
)


SOURCE_ASSERTIONS: Mapping[str, tuple[str, ...]] = {
    "vera/machine learning/ml_onnx.py": (
        'cap_name = f"ml.onnx.model.{slug}"',
        '"ml.onnx.run"',
        "return await _run_artifact(artifact, X)",
        "_rescan_and_register()",
        'CAPABILITY_REGISTRY.pop(f"ml.onnx.model.{slug}", None)',
    ),
    "vera/models/legacy_binding.py": (
        'LegacyModelCapabilityBinding("ml.onnx.run", slug, package_id, source)',
        'LegacyModelCapabilityBinding(f"ml.onnx.model.{slug}", "", package_id, source)',
    ),
    "vera/models/onnx_import.py": (
        "registry.register(package)",
        'return ONNXImportReceipt(package.package_id, "registered", tuple(receipts))',
    ),
    "vera/inventory/system_inventory.py": (
        '"artifacts": _artifact_provider_records(cap_items)',
        '"scope": "provider_surface_not_stored_content"',
    ),
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
    missing = [assertion for assertion in assertions if assertion not in text]
    if missing:
        raise ValueError(f"source assertions are missing from {relative}: {missing}")
    return {
        "source_path_digest": "sha256:" + hashlib.sha256(relative.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "assertion_digests": [
            "sha256:" + hashlib.sha256(assertion.encode()).hexdigest()
            for assertion in assertions
        ],
    }


def build_onnx_capability_authority_review(
    repo_root: Path,
    surfaces: Sequence[ONNXIdentitySurface] = CURRENT_SURFACES,
    source_assertions: Mapping[str, Sequence[str]] = SOURCE_ASSERTIONS,
) -> dict[str, Any]:
    """Describe the current authority split without loading or running a model."""
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface_id for item in surfaces}) != len(surfaces):
        raise ValueError("ONNX identity surfaces must be non-empty and unique")
    evidence = {
        source: _source_evidence(Path(repo_root), source, assertions)
        for source, assertions in sorted(source_assertions.items())
    }
    payload = {
        "schema": SCHEMA,
        "surfaces": [surface.to_dict() for surface in surfaces],
        "source_evidence": evidence,
        "authority": {
            "stable_execution": "ml.onnx.run",
            "artifact_identity": "ModelPackage",
            "artifact_catalog": "model-package-registry",
            "compatibility_invocation": "ml.onnx.model.<slug>",
        },
        "findings": [
            "artifact_identity_is_already_representable_as_data",
            "dynamic_capabilities_duplicate_the_stable_executor_per_artifact",
            "dynamic_capabilities_are_restored_from_manifest_state",
            "legacy_bindings_preserve_both_current_invocation_identities",
            "system_inventory_does_not_inventory_model_package_instances",
        ],
        "recommendations": [
            "keep_both_invocation_forms_callable_during_compatibility_period",
            "treat_model_packages_as_authoritative_artifact_identity",
            "prefer_stable_executor_plus_artifact_selector_for_new_integrations",
            "surface_model_package_records_in_inventory_before_namespace_change",
            "measure_stored_external_and_runtime_consumers_before_any_retirement",
            "require_inference_parity_evidence_before_runtime_delegation",
        ],
        "coverage": {
            "source_registration_and_identity": "complete_for_declared_surfaces",
            "stored_consumers": "not_examined",
            "external_consumers": "not_examined",
            "runtime_calls": "not_examined",
            "inference_parity": "not_run",
        },
        "removal_authority": False,
        "changes_registration": False,
        "changes_routing": False,
        "loads_models": False,
        "executes_inference": False,
        "mutates": False,
    }
    return {"review_id": _identity("ocar_", payload), **payload}
