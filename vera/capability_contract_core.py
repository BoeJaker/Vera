"""Deterministic Capability Contract v2 projection and linting.

The existing registry remains the execution source of truth.  This module only
projects it into a richer, explicit inspection format so metadata can be filled
incrementally without changing dispatch behaviour.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


CONTRACT_SCHEMA = "vera.capability-contract/v2"
MANIFEST_SET_SCHEMA = "vera.capability-contract-set/v2"
LINT_SCHEMA = "vera.capability-contract-lint/v2"
COVERAGE_SCHEMA = "vera.capability-contract-coverage/v2"
GATE_SCHEMA = "vera.capability-contract-gate/v2"
LIFECYCLES = {"active", "deprecated", "experimental", "internal", "removed"}
EFFECTS = {
    "none", "read", "write", "delete", "execute", "network", "filesystem",
    "secrets", "approval", "model", "accelerator", "external_side_effect",
}
_SECRET_NAME = re.compile(r"(?:^|_)(?:password|passwd|secret|token|api_key|private_key)(?:$|_)", re.I)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GATE_REQUIRED_DIMENSIONS = (
    "contract", "output_schema", "effects", "owner", "approval", "secrets",
    "filesystem", "network", "tenant", "idempotency", "resources",
)


def _json_copy(value: Any, fallback: Any) -> Any:
    if value is None:
        return copy.deepcopy(fallback)
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        return copy.deepcopy(fallback)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({_text(item) for item in value if _text(item)})


def _unknown(status: str = "unknown") -> dict[str, str]:
    return {"status": status}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def project_contract(name: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project one legacy registry entry into a stable v2 manifest."""
    declared = entry.get("contract") if isinstance(entry.get("contract"), Mapping) else {}
    input_schema = _json_copy(entry.get("schema"), {"type": "object", "properties": {}})
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    input_schema.setdefault("type", "object")
    input_schema.setdefault("properties", {})

    lifecycle = _text(declared.get("lifecycle")) or "active"
    effects = _list(declared.get("effects"))
    effect_status = "declared" if "effects" in declared else "unknown"
    aliases = _list(declared.get("aliases"))
    canonical_task = _text(declared.get("canonical_task")) or name
    output_schema = _json_copy(declared.get("output_schema"), {})
    owner = _text(declared.get("owner"))

    manifest = {
        "schema": CONTRACT_SCHEMA,
        "name": name,
        "canonical_task": canonical_task,
        "implementation": {
            "name": name,
            "mode": _text(entry.get("mode")) or "local",
            "source": _text(entry.get("source")) or "local",
            "server": _text(entry.get("server")),
        },
        "aliases": aliases,
        "lifecycle": lifecycle,
        "deprecation": {
            "replacement": _text(declared.get("replacement")),
            "sunset": _text(declared.get("sunset")),
            "reason": _text(declared.get("deprecation_reason")),
        },
        "declaration": {
            "status": "declared" if declared else "legacy_projected",
            "fields": sorted(str(key) for key in declared),
        },
        "schemas": {
            "input": input_schema,
            "output": output_schema,
            "output_status": "declared" if output_schema else "unknown",
        },
        "effects": {"status": effect_status, "declared": effects},
        "policy": {
            "approval": _json_copy(declared.get("approval"), _unknown()),
            "trust": _json_copy(declared.get("trust"), _unknown()),
            "secrets": _json_copy(declared.get("secrets"), _unknown()),
            "filesystem": _json_copy(declared.get("filesystem"), _unknown()),
            "network": _json_copy(declared.get("network"), _unknown()),
            "tenant": _json_copy(declared.get("tenant"), _unknown()),
        },
        "execution": {
            "streams": _list(entry.get("streams")),
            "timeout_ms": _json_copy(declared.get("timeout_ms"), None),
            "retries": _nonnegative_int(entry.get("retries")),
            "idempotency": _json_copy(declared.get("idempotency"), _unknown()),
            "cancellation": _json_copy(declared.get("cancellation"), _unknown()),
            "pagination": _json_copy(declared.get("pagination"), _unknown()),
        },
        "quality": {
            "health": _json_copy(declared.get("health"), _unknown()),
            "cost": _json_copy(declared.get("cost"), _unknown()),
            "latency": _json_copy(declared.get("latency"), _unknown()),
            "quality": _json_copy(declared.get("quality"), _unknown()),
        },
        "resources": _json_copy(declared.get("resources"), _unknown()),
        "provenance": {
            "owner": owner,
            "description": _text(entry.get("description")),
            "tags": _list(entry.get("tags")),
            "http": {
                "method": _text(entry.get("http_method")),
                "path": _text(entry.get("http_path")),
            },
            "mcp_exposed": bool(entry.get("mcp_expose", True)),
        },
    }
    return manifest


def project_registry(registry: Mapping[str, Mapping[str, Any]], *,
                     include_internal: bool = False) -> list[dict[str, Any]]:
    manifests = []
    for name in sorted(registry):
        entry = registry[name]
        if not isinstance(entry, Mapping):
            continue
        if not include_internal and not entry.get("mcp_expose", True):
            continue
        manifests.append(project_contract(name, entry))
    return manifests


def manifest_fingerprint(manifests: list[Mapping[str, Any]]) -> str:
    ordered = sorted((_json_copy(item, {}) for item in manifests),
                     key=lambda item: _text(item.get("name")))
    payload = json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coverage_presence(manifest: Mapping[str, Any]) -> dict[str, bool]:
    declared_fields = set(_mapping(manifest.get("declaration")).get("fields") or [])
    schemas = _mapping(manifest.get("schemas"))
    effects = _mapping(manifest.get("effects"))
    policy = _mapping(manifest.get("policy"))
    execution = _mapping(manifest.get("execution"))
    quality = _mapping(manifest.get("quality"))
    provenance = _mapping(manifest.get("provenance"))
    checks = {
        "contract": (_mapping(manifest.get("declaration")).get("status") == "declared"
                     and bool(declared_fields)),
        "output_schema": schemas.get("output_status") == "declared",
        "effects": effects.get("status") == "declared",
        "owner": bool(_text(provenance.get("owner"))),
        "approval": _mapping(policy.get("approval")).get("status") != "unknown",
        "trust": _mapping(policy.get("trust")).get("status") != "unknown",
        "secrets": _mapping(policy.get("secrets")).get("status") != "unknown",
        "filesystem": _mapping(policy.get("filesystem")).get("status") != "unknown",
        "network": _mapping(policy.get("network")).get("status") != "unknown",
        "tenant": _mapping(policy.get("tenant")).get("status") != "unknown",
        "idempotency": _mapping(execution.get("idempotency")).get("status") != "unknown",
        "cancellation": _mapping(execution.get("cancellation")).get("status") != "unknown",
        "pagination": _mapping(execution.get("pagination")).get("status") != "unknown",
        "health": _mapping(quality.get("health")).get("status") != "unknown",
        "cost": _mapping(quality.get("cost")).get("status") != "unknown",
        "latency": _mapping(quality.get("latency")).get("status") != "unknown",
        "quality": _mapping(quality.get("quality")).get("status") != "unknown",
        "resources": _mapping(manifest.get("resources")).get("status") != "unknown",
    }
    return checks


def contract_coverage(manifests: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Measure migration completeness without treating inferred defaults as declarations."""
    dimensions = (
        "contract", "output_schema", "effects", "owner", "approval", "trust",
        "secrets", "filesystem", "network", "tenant", "idempotency",
        "cancellation", "pagination", "health", "cost", "latency", "quality",
        "resources",
    )
    groups: dict[str, dict[str, Any]] = {}
    hotspots = []
    totals = {dimension: 0 for dimension in dimensions}
    ordered = sorted(manifests, key=lambda item: _text(item.get("name")))
    for manifest in ordered:
        name = _text(manifest.get("name"))
        declared_fields = set(_mapping(manifest.get("declaration")).get("fields") or [])
        checks = _coverage_presence(manifest)
        missing = [dimension for dimension in dimensions if not checks[dimension]]
        for dimension, present in checks.items():
            totals[dimension] += int(present)
        group = name.split(".", 1)[0] if name else "unknown"
        bucket = groups.setdefault(group, {"group": group, "total": 0,
                                           "declared": 0, "missing_fields": 0})
        bucket["total"] += 1
        bucket["declared"] += int(checks["contract"])
        bucket["missing_fields"] += len(missing)
        hotspots.append({"name": name, "group": group, "missing": missing,
                         "missing_count": len(missing),
                         "declared_fields": sorted(declared_fields)})

    count = len(ordered)
    coverage = {
        dimension: {"declared": totals[dimension], "total": count,
                    "rate": round(totals[dimension] / count, 4) if count else 0.0}
        for dimension in dimensions
    }
    group_rows = []
    for bucket in groups.values():
        bucket["declaration_rate"] = round(
            bucket["declared"] / bucket["total"], 4) if bucket["total"] else 0.0
        group_rows.append(bucket)
    return {
        "schema": COVERAGE_SCHEMA,
        "manifests": count,
        "coverage": coverage,
        "groups": sorted(group_rows, key=lambda row: (-row["missing_fields"], row["group"])),
        "hotspots": sorted(hotspots, key=lambda row: (-row["missing_count"], row["name"])),
    }


def gate_contracts(manifests: list[Mapping[str, Any]], *,
                   required_dimensions: tuple[str, ...] = GATE_REQUIRED_DIMENSIONS,
                   fail_on_warnings: bool = False) -> dict[str, Any]:
    """Strictly gate only an explicitly selected migration set."""
    issues = lint_contracts(manifests)
    for manifest in sorted(manifests, key=lambda item: _text(item.get("name"))):
        name = _text(manifest.get("name")) or "<unnamed>"
        presence = _coverage_presence(manifest)
        for dimension in required_dimensions:
            if dimension not in presence:
                issues.append(_issue(name, "gate.dimension_unknown", "error", dimension,
                                     f"unknown required gate dimension '{dimension}'"))
            elif not presence[dimension]:
                issues.append(_issue(name, "gate.declaration_missing", "error", dimension,
                                     f"required contract dimension '{dimension}' is undeclared"))
    issues = sorted(issues, key=lambda issue: (issue["severity"], issue["code"],
                                                issue["name"], issue["path"]))
    errors = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {"schema": GATE_SCHEMA, "manifests": len(manifests),
            "required_dimensions": list(required_dimensions), "issues": issues,
            "counts": {"error": errors, "warning": warnings},
            "fail_on_warnings": bool(fail_on_warnings),
            "ok": errors == 0 and (warnings == 0 or not fail_on_warnings)}


def _issue(name: str, code: str, severity: str, path: str, message: str) -> dict[str, str]:
    return {"name": name, "code": code, "severity": severity,
            "path": path, "message": message}


def lint_contracts(manifests: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Return stable, actionable issues; never infer missing declarations as facts."""
    issues: list[dict[str, str]] = []
    alias_owners: dict[str, set[str]] = {}
    manifest_names = {_text(item.get("name")) for item in manifests if _text(item.get("name"))}
    for manifest in sorted(manifests, key=lambda item: _text(item.get("name"))):
        name = _text(manifest.get("name")) or "<unnamed>"
        lifecycle = _text(manifest.get("lifecycle"))
        if lifecycle not in LIFECYCLES:
            issues.append(_issue(name, "lifecycle.invalid", "error", "lifecycle",
                                 f"unknown lifecycle '{lifecycle}'"))
        deprecation = _mapping(manifest.get("deprecation"))
        replacement = _text(deprecation.get("replacement"))
        sunset = _text(deprecation.get("sunset"))
        reason = _text(deprecation.get("reason"))
        if lifecycle == "deprecated":
            if not replacement or replacement == name:
                issues.append(_issue(name, "lifecycle.replacement_missing", "error",
                                     "deprecation.replacement",
                                     "deprecated capability requires a different replacement"))
            if not sunset or not _ISO_DATE.match(sunset):
                issues.append(_issue(name, "lifecycle.sunset_invalid", "error",
                                     "deprecation.sunset",
                                     "deprecated capability requires an ISO YYYY-MM-DD sunset"))
            if not reason:
                issues.append(_issue(name, "lifecycle.reason_missing", "error",
                                     "deprecation.reason",
                                     "deprecated capability requires a reason"))
        elif any((replacement, sunset, reason)):
            issues.append(_issue(name, "lifecycle.deprecation_inactive", "warning",
                                 "deprecation",
                                 "deprecation metadata is set but lifecycle is not deprecated"))
        if lifecycle == "removed" and bool(_mapping(manifest.get("provenance")).get("mcp_exposed")):
            issues.append(_issue(name, "lifecycle.removed_exposed", "error",
                                 "provenance.mcp_exposed",
                                 "removed capability must not remain MCP-exposed"))

        schemas = manifest.get("schemas") if isinstance(manifest.get("schemas"), Mapping) else {}
        input_schema = schemas.get("input") if isinstance(schemas.get("input"), Mapping) else {}
        properties = input_schema.get("properties") if isinstance(input_schema.get("properties"), Mapping) else {}
        required = input_schema.get("required") if isinstance(input_schema.get("required"), list) else []
        for field in sorted(set(required) - set(properties)):
            issues.append(_issue(name, "schema.required_unknown", "error",
                                 f"schemas.input.required.{field}",
                                 "required input is absent from properties"))
        for field, spec in sorted(properties.items()):
            if _SECRET_NAME.search(str(field)) and isinstance(spec, Mapping):
                secret_ref = spec.get("format") == "secret-ref" or spec.get("x-vera-secret-ref") is True
                if spec.get("type") == "string" and not secret_ref:
                    issues.append(_issue(name, "schema.secret_plaintext", "error",
                                         f"schemas.input.properties.{field}",
                                         "secret-like string input must use an opaque secret reference"))

        effects = manifest.get("effects") if isinstance(manifest.get("effects"), Mapping) else {}
        declared_effects = _list(effects.get("declared"))
        for effect in sorted(set(declared_effects) - EFFECTS):
            issues.append(_issue(name, "effects.invalid", "error", "effects.declared",
                                 f"unknown effect '{effect}'"))
        method = _text(_mapping(_mapping(manifest.get("provenance")).get("http")).get("method")).upper()
        if effects.get("status") != "declared" and method in {"POST", "PUT", "PATCH", "DELETE"}:
            issues.append(_issue(name, "effects.undeclared", "warning", "effects",
                                 f"{method} capability has no explicit effect declaration"))

        source = _text(_mapping(manifest.get("implementation")).get("source"))
        if source == "mcp_proxy" and _text(manifest.get("canonical_task")) == name:
            issues.append(_issue(name, "provider.unmapped_task", "warning", "canonical_task",
                                 "provider implementation is presented as its own user task"))
        for alias in manifest.get("aliases") or []:
            alias_owners.setdefault(_text(alias), set()).add(name)

    for alias, owners in sorted(alias_owners.items()):
        conflicts = set(owners)
        if alias in manifest_names:
            conflicts.add(alias)
        if alias and len(conflicts) > 1:
            for owner in sorted(owners):
                issues.append(_issue(owner, "alias.ambiguous", "error", "aliases",
                                     f"alias '{alias}' is also declared by " +
                                     ", ".join(sorted(conflicts - {owner}))))
    return sorted(issues, key=lambda issue: (issue["severity"], issue["code"],
                                              issue["name"], issue["path"]))
