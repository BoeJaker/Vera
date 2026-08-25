"""Deterministic, non-executing capability resolution previews.

Shadow resolution consumes Capability Contract v2 manifests and optional redacted
observations.  It never calls a capability and never grants authorization.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


RESOLUTION_SCHEMA = "vera.capability-resolution-shadow/v1"
POLICY_DIMENSIONS = {"approval", "trust", "secrets", "filesystem", "network", "tenant"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {_text(item) for item in value if _text(item)}


def _observation_index(observations: list[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    return {_text(row.get("name")): row for row in (observations or [])
            if isinstance(row, Mapping) and _text(row.get("name"))}


def _metric(value: Any, *, minimum: float = 0.0,
            maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _evidence_metric(observation: Mapping[str, Any], observation_dimension: str,
                     observation_field: str, manifest: Mapping[str, Any],
                     contract_dimension: str, contract_field: str, *,
                     maximum: float | None = None) -> tuple[float | None, str | None]:
    observed = _mapping(observation.get(observation_dimension))
    if observed.get("status") == "observed":
        value = _metric(observed.get(observation_field), maximum=maximum)
        if value is not None:
            return value, "observation"
    declared = _mapping(_mapping(manifest.get("quality")).get(contract_dimension))
    if declared.get("status") in {"declared", "observed"}:
        value = _metric(declared.get(contract_field), maximum=maximum)
        if value is not None:
            return value, "contract"
    return None, None


def _policy_requirements(value: Any) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    if value is None:
        return {}, []
    if not isinstance(value, Mapping):
        return {}, [{"code": "request.policy_requirements_invalid"}]
    requirements = {}
    issues = []
    for dimension in sorted(value):
        if dimension not in POLICY_DIMENSIONS:
            issues.append({"code": "request.policy_dimension_unknown",
                           "dimension": _text(dimension)})
            continue
        raw = value[dimension]
        allowed = {_text(raw)} if isinstance(raw, str) else _string_set(raw)
        if not allowed:
            issues.append({"code": "request.policy_statuses_empty", "dimension": dimension})
            continue
        requirements[dimension] = allowed
    return requirements, issues


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    value = schema.get("type")
    return {_text(value)} if isinstance(value, str) else _string_set(value)


def _output_schema_exclusions(manifest: Mapping[str, Any], required: Mapping[str, Any]) -> list[dict]:
    schemas = _mapping(manifest.get("schemas"))
    if schemas.get("output_status") != "declared":
        return [{"code": "output_schema_unknown"}]
    actual = _mapping(schemas.get("output"))
    expected_types = _schema_types(required)
    actual_types = _schema_types(actual)
    reasons = []
    if expected_types and (not actual_types or expected_types.isdisjoint(actual_types)):
        reasons.append({"code": "output_type_mismatch", "expected": sorted(expected_types),
                        "actual": sorted(actual_types) or ["unknown"]})

    expected_required = _string_set(required.get("required"))
    actual_required = _string_set(actual.get("required"))
    missing = sorted(expected_required - actual_required)
    if missing:
        reasons.append({"code": "output_required_missing", "actual": missing})

    expected_properties = _mapping(required.get("properties"))
    actual_properties = _mapping(actual.get("properties"))
    for name in sorted(expected_properties)[:100]:
        expected = _schema_types(_mapping(expected_properties.get(name)))
        actual_value = actual_properties.get(name)
        actual_property_types = _schema_types(_mapping(actual_value))
        if expected and (actual_value is None or not actual_property_types
                         or expected.isdisjoint(actual_property_types)):
            reasons.append({"code": "output_property_type_mismatch", "property": name,
                            "expected": sorted(expected),
                            "actual": sorted(actual_property_types) or ["unknown"]})
    return reasons


def resolve_shadow(manifests: list[Mapping[str, Any]], request: Mapping[str, Any], *,
                   observations: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Explain eligibility and rank candidates without invoking or authorizing them."""
    task = _text(request.get("canonical_task"))
    allowed_effects = _string_set(request.get("allowed_effects"))
    required_resources = _string_set(request.get("required_resources"))
    preferred = [_text(item) for item in request.get("preferred") or [] if _text(item)]
    preference = {name: index for index, name in enumerate(preferred)}
    observed = _observation_index(observations)
    candidates = []
    policy_requirements, request_issues = _policy_requirements(
        request.get("policy_requirements"))
    output_schema = request.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, Mapping):
        request_issues.append({"code": "request.output_schema_invalid"})
        output_schema = None
    elif isinstance(output_schema, Mapping):
        properties = _mapping(output_schema.get("properties"))
        required = output_schema.get("required")
        if len(properties) > 100 or (isinstance(required, (list, tuple, set))
                                     and len(required) > 100):
            request_issues.append({"code": "request.output_schema_too_large",
                                   "max_properties": 100})

    try:
        candidate_limit = max(1, min(int(request.get("candidate_limit") or 100), 500))
    except (TypeError, ValueError):
        candidate_limit = 100
    family = [manifest for manifest in manifests
              if _text(manifest.get("canonical_task")) == task]
    family.sort(key=lambda item: _text(item.get("name")))

    if request_issues:
        return {
            "schema": RESOLUTION_SCHEMA, "mode": "shadow", "authorized": False,
            "executed": False, "request": {"canonical_task": task},
            "request_issues": request_issues, "selected": None, "eligible": [],
            "excluded": [], "counts": {"matched": len(family), "considered": 0,
                                        "eligible": 0, "excluded": 0},
            "truncated": bool(family),
        }

    for manifest in family[:candidate_limit]:
        name = _text(manifest.get("name"))
        reasons = []
        lifecycle = _text(manifest.get("lifecycle"))
        if lifecycle not in {"active", "experimental"}:
            reasons.append({"code": "lifecycle_ineligible", "actual": lifecycle or "unknown"})
        effects = _mapping(manifest.get("effects"))
        declared_effects = _string_set(effects.get("declared"))
        if effects.get("status") != "declared":
            reasons.append({"code": "effects_unknown"})
        elif allowed_effects and not declared_effects <= allowed_effects:
            reasons.append({"code": "effects_not_allowed",
                            "actual": sorted(declared_effects - allowed_effects)})

        resources = _mapping(manifest.get("resources"))
        classes = _string_set(resources.get("classes"))
        if required_resources and resources.get("status") != "declared":
            reasons.append({"code": "resources_unknown"})
        elif not required_resources <= classes:
            reasons.append({"code": "resources_missing",
                            "actual": sorted(required_resources - classes)})

        if output_schema is not None:
            reasons.extend(_output_schema_exclusions(manifest, output_schema))

        policy = _mapping(manifest.get("policy"))
        for dimension, allowed_statuses in policy_requirements.items():
            actual_status = _text(_mapping(policy.get(dimension)).get("status"))
            if not actual_status or actual_status == "unknown":
                reasons.append({"code": "policy_unknown", "dimension": dimension})
            elif actual_status not in allowed_statuses:
                reasons.append({"code": "policy_mismatch", "dimension": dimension,
                                "expected": sorted(allowed_statuses),
                                "actual": actual_status})

        observation = observed.get(name, {})
        health = _mapping(observation.get("health"))
        if health.get("status") == "observed" and health.get("healthy") is False:
            reasons.append({"code": "observed_unhealthy"})

        reliability_value = _metric(observation.get("success_rate"), maximum=1.0)
        reliability = reliability_value if reliability_value is not None else -1.0
        quality_value, quality_source = _evidence_metric(
            observation, "quality", "score", manifest, "quality", "score", maximum=1.0)
        latency_value, latency_source = _evidence_metric(
            observation, "latency_ms", "p95", manifest, "latency", "p95_ms")
        cost_value, cost_source = _evidence_metric(
            observation, "cost", "normalized_per_call",
            manifest, "cost", "normalized_per_call")
        load_observation = _mapping(observation.get("load"))
        load_value = (_metric(load_observation.get("utilization"), maximum=1.0)
                      if load_observation.get("status") == "observed" else None)
        load_source = "observation" if load_value is not None else None
        if load_value is None and resources.get("status") == "declared":
            load_value = _metric(resources.get("utilization"), maximum=1.0)
            load_source = "contract" if load_value is not None else None
        quality_rank = quality_value if quality_value is not None else -1.0
        latency_rank = latency_value if latency_value is not None else float("inf")
        cost_rank = cost_value if cost_value is not None else float("inf")
        load_rank = load_value if load_value is not None else float("inf")
        implementation = _mapping(manifest.get("implementation"))
        rank = {
            "preference": preference.get(name, len(preferred)),
            "reliability": reliability_value,
            "quality": quality_value,
            "latency_p95_ms": latency_value,
            "cost_normalized_per_call": cost_value,
            "load_utilization": load_value,
            "local": implementation.get("mode") == "local",
            "evidence_sources": {
                "reliability": "observation" if reliability_value is not None else None,
                "quality": quality_source,
                "latency": latency_source,
                "cost": cost_source,
                "load": load_source,
            },
        }
        sort_key = (rank["preference"], -reliability, -quality_rank,
                    latency_rank, cost_rank, load_rank,
                    0 if rank["local"] else 1, name)
        candidates.append({"name": name, "eligible": not reasons,
                           "exclusions": reasons, "rank": rank, "_sort": sort_key})

    eligible = sorted((row for row in candidates if row["eligible"]), key=lambda row: row["_sort"])
    excluded = sorted((row for row in candidates if not row["eligible"]), key=lambda row: row["name"])
    for row in eligible + excluded:
        row.pop("_sort", None)
    return {
        "schema": RESOLUTION_SCHEMA,
        "mode": "shadow",
        "authorized": False,
        "executed": False,
        "request": {"canonical_task": task, "allowed_effects": sorted(allowed_effects),
                    "required_resources": sorted(required_resources), "preferred": preferred,
                    "candidate_limit": candidate_limit,
                    "output_schema": dict(output_schema) if output_schema is not None else None,
                    "policy_requirements": {key: sorted(value)
                                            for key, value in policy_requirements.items()}},
        "request_issues": [],
        "selected": eligible[0]["name"] if eligible else None,
        "eligible": eligible,
        "excluded": excluded,
        "counts": {"matched": len(family), "considered": len(candidates),
                   "eligible": len(eligible), "excluded": len(excluded)},
        "truncated": len(family) > len(candidates),
    }
