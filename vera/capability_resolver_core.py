"""Deterministic, non-executing capability resolution previews.

Shadow resolution consumes Capability Contract v2 manifests and optional redacted
observations.  It never calls a capability and never grants authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


RESOLUTION_SCHEMA = "vera.capability-resolution-shadow/v1"


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

    for manifest in sorted(manifests, key=lambda item: _text(item.get("name"))):
        name = _text(manifest.get("name"))
        reasons = []
        lifecycle = _text(manifest.get("lifecycle"))
        if lifecycle not in {"active", "experimental"}:
            reasons.append({"code": "lifecycle_ineligible", "actual": lifecycle or "unknown"})
        if not task or _text(manifest.get("canonical_task")) != task:
            reasons.append({"code": "task_mismatch",
                            "actual": _text(manifest.get("canonical_task"))})

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

        observation = observed.get(name, {})
        health = _mapping(observation.get("health"))
        if health.get("status") == "observed" and health.get("healthy") is False:
            reasons.append({"code": "observed_unhealthy"})

        latency = _mapping(observation.get("latency_ms"))
        success_rate = observation.get("success_rate")
        reliability = float(success_rate) if isinstance(success_rate, (int, float)) else -1.0
        p95 = latency.get("p95")
        latency_rank = float(p95) if isinstance(p95, (int, float)) else float("inf")
        implementation = _mapping(manifest.get("implementation"))
        rank = {
            "preference": preference.get(name, len(preferred)),
            "reliability": reliability if reliability >= 0 else None,
            "latency_p95_ms": latency_rank if latency_rank != float("inf") else None,
            "local": implementation.get("mode") == "local",
        }
        sort_key = (rank["preference"], -reliability,
                    latency_rank, 0 if rank["local"] else 1, name)
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
                    "required_resources": sorted(required_resources), "preferred": preferred},
        "selected": eligible[0]["name"] if eligible else None,
        "eligible": eligible,
        "excluded": excluded,
        "counts": {"considered": len(candidates), "eligible": len(eligible),
                   "excluded": len(excluded)},
    }
