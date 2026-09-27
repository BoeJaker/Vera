"""Deterministic read projection across Vera's portable model records.

This module joins already-observed records.  It never discovers hardware,
loads a model, hashes an artifact, admits a package, or selects a provider.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .model_package import ModelPackage, model_package_from_dict


MODEL_INVENTORY_SCHEMA = "vera.model-inventory/v1"
_EXTERNAL_SCHEMAS = {"vera.nlp-model-inventory/v1"}


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError(f"{label} must be a mapping or expose to_dict()")


def _package_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, ModelPackage):
        return value.to_dict()
    return model_package_from_dict(_as_dict(value, "package")).to_dict()


def project_model_inventory(
    *,
    packages: Iterable[Any] = (),
    aliases: Iterable[Mapping[str, Any]] = (),
    admissions: Iterable[Any] = (),
    activations: Iterable[Any] = (),
    legacy_bindings: Iterable[Any] = (),
    deployments: Iterable[Any] = (),
    observations: Iterable[Any] = (),
    providers: Iterable[Any] = (),
    external_inventories: Iterable[tuple[str, Mapping[str, Any]]] = (),
    source_status: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one stable inventory from persisted and source-owned records.

    Unknown references remain visible as conflicts.  External candidates are
    never promoted into packages; only schema-valid ``ModelPackage`` payloads
    enter the package list.
    """

    package_values: dict[str, dict[str, Any]] = {}
    package_sources: dict[str, set[str]] = defaultdict(set)
    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []

    def add_package(raw: Any, source: str) -> None:
        try:
            value = _package_dict(raw)
        except (TypeError, ValueError) as exc:
            conflicts.append({
                "kind": "invalid_package",
                "source": source,
                "reason": f"{type(exc).__name__}:{exc}",
            })
            return
        package_id = value["package_id"]
        current = package_values.get(package_id)
        if current is not None and current != value:
            conflicts.append({
                "kind": "package_identity_collision",
                "source": source,
                "package_id": package_id,
                "reason": "same package_id has different canonical content",
            })
            return
        package_values[package_id] = value
        package_sources[package_id].add(source)

    for package in packages:
        add_package(package, "registry")

    external_sources: list[str] = []
    for source_id, raw_inventory in external_inventories:
        source_id = str(source_id or "").strip()
        if not source_id:
            raise ValueError("external inventory source ID is required")
        if not isinstance(raw_inventory, Mapping):
            raise TypeError("external inventory must be a mapping")
        external_sources.append(source_id)
        if raw_inventory.get("schema") not in _EXTERNAL_SCHEMAS:
            conflicts.append({
                "kind": "unsupported_external_inventory",
                "source": source_id,
                "reason": str(raw_inventory.get("schema") or "missing_schema"),
            })
            continue
        for package in raw_inventory.get("packages") or ():
            add_package(package, source_id)
        for candidate in raw_inventory.get("candidates") or ():
            if not isinstance(candidate, Mapping):
                conflicts.append({
                    "kind": "invalid_candidate",
                    "source": source_id,
                    "reason": "candidate is not a mapping",
                })
                continue
            candidates.append({"source": source_id, **dict(candidate)})
        for conflict in raw_inventory.get("conflicts") or ():
            detail = dict(conflict) if isinstance(conflict, Mapping) else {
                "reason": str(conflict)
            }
            conflicts.append({
                "kind": "external_conflict",
                "source": source_id,
                "reason": str(detail.get("reason") or detail.get("kind") or
                              "source_reported_conflict"),
            })

    aliases_by_package: dict[str, list[str]] = defaultdict(list)
    normalized_aliases: list[dict[str, str]] = []
    for raw in aliases:
        value = _as_dict(raw, "alias")
        alias = str(value.get("alias") or "")
        package_id = str(value.get("package_id") or "")
        if not alias or not package_id:
            raise ValueError("alias requires alias and package_id")
        normalized_aliases.append({"alias": alias, "package_id": package_id})
        aliases_by_package[package_id].append(alias)
        if package_id not in package_values:
            conflicts.append({"kind": "dangling_alias", "source": "registry",
                              "package_id": package_id, "reason": alias})

    def normalize(values: Iterable[Any], label: str) -> list[dict[str, Any]]:
        return [_as_dict(value, label) for value in values]

    normalized_admissions = normalize(admissions, "admission")
    normalized_activations = normalize(activations, "activation")
    normalized_bindings = normalize(legacy_bindings, "legacy binding")
    normalized_deployments = normalize(deployments, "deployment")
    normalized_observations = normalize(observations, "observation")
    normalized_providers = normalize(providers, "provider")

    def group(values: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for value in values:
            identity = str(value.get(key) or "")
            if identity:
                grouped[identity].append(value)
                if key == "package_id" and identity not in package_values:
                    conflicts.append({
                        "kind": "unknown_package_reference",
                        "source": "registry",
                        "package_id": identity,
                        "reason": str(value.get("schema") or label_for(value)),
                    })
        return grouped

    def label_for(value: Mapping[str, Any]) -> str:
        return str(value.get("deployment_id") or value.get("admission_id") or
                   value.get("operation_id") or value.get("provider_id") or "record")

    admissions_by_package = group(normalized_admissions, "package_id")
    activations_by_package = group(normalized_activations, "package_id")
    deployments_by_package = group(normalized_deployments, "package_id")
    bindings_by_package = group(normalized_bindings, "package_id")
    providers_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for provider in normalized_providers:
        for package_id in provider.get("package_ids") or ():
            package_id = str(package_id)
            providers_by_package[package_id].append(provider)
            if package_id not in package_values:
                conflicts.append({
                    "kind": "unknown_provider_package",
                    "source": "provider_registry",
                    "package_id": package_id,
                    "reason": str(provider.get("provider_id") or "provider"),
                })

    observation_by_deployment = {
        str(value.get("deployment_id")): value for value in normalized_observations
        if value.get("deployment_id")
    }
    entries = []
    for package_id in sorted(package_values):
        package_deployments = []
        for deployment in deployments_by_package.get(package_id, ()):
            deployment = dict(deployment)
            observation = observation_by_deployment.get(
                str(deployment.get("deployment_id") or ""))
            if observation is not None:
                deployment["observation"] = observation
            package_deployments.append(deployment)
        entries.append({
            "package": package_values[package_id],
            "sources": sorted(package_sources[package_id]),
            "aliases": sorted(set(aliases_by_package.get(package_id, ()))),
            "admissions": sorted(admissions_by_package.get(package_id, ()),
                                 key=lambda value: str(value.get("admission_id") or "")),
            "activations": sorted(activations_by_package.get(package_id, ()),
                                  key=lambda value: int(value.get("sequence") or 0)),
            "deployments": sorted(package_deployments,
                                  key=lambda value: str(value.get("deployment_id") or "")),
            "providers": sorted(providers_by_package.get(package_id, ()),
                                key=lambda value: str(value.get("provider_id") or "")),
            "legacy_bindings": sorted(bindings_by_package.get(package_id, ()),
                                      key=lambda value: (str(value.get("capability") or ""),
                                                         str(value.get("selector") or ""))),
        })

    candidates.sort(key=lambda value: (
        str(value.get("source") or ""), str(value.get("task") or ""),
        str(value.get("model") or "")))
    conflicts.sort(key=lambda value: (
        value.get("kind", ""), value.get("source", ""),
        value.get("package_id", ""), value.get("reason", "")))
    return {
        "schema": MODEL_INVENTORY_SCHEMA,
        "packages": entries,
        "candidates": candidates,
        "aliases": sorted(normalized_aliases,
                          key=lambda value: (value["alias"], value["package_id"])),
        "activations": sorted(normalized_activations,
                              key=lambda value: int(value.get("sequence") or 0)),
        "conflicts": conflicts,
        "sources": {
            key: dict(value) for key, value in sorted((source_status or {}).items())
        },
        "counts": {
            "packages": len(entries),
            "candidates": len(candidates),
            "aliases": len(normalized_aliases),
            "deployments": len(normalized_deployments),
            "providers": len(normalized_providers),
            "conflicts": len(conflicts),
            "external_sources": len(set(external_sources)),
        },
    }
