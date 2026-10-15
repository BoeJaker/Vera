"""Read-only projection of the models the Intelligence subsystem actually consumes."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA = "vera.intel-model-consumers/v1"

NLP_CONSUMERS = (
    {"consumer_id": "intel.triage.embed", "cap": "nlp.embed", "selector": {}},
    {"consumer_id": "intel.triage.rerank", "cap": "nlp.rerank", "selector": {}},
    {"consumer_id": "intel.triage.genre", "cap": "nlp.zeroshot", "selector": {}},
    {"consumer_id": "intel.enrich.entities", "cap": "nlp.ner", "selector": {"task": "gliner"}},
    {"consumer_id": "intel.enrich.qa", "cap": "nlp.qa", "selector": {}},
    {"consumer_id": "intel.enrich.sentiment", "cap": "nlp.classify",
     "selector": {"task": "sentiment3"}},
)


def _selector_matches(actual: Any, wanted: Mapping[str, Any]) -> bool:
    actual = actual if isinstance(actual, Mapping) else {}
    return actual == wanted


def _packages(row: Mapping[str, Any]) -> list[str]:
    values = list(row.get("package_ids") or [])
    if row.get("package_id"):
        values.append(row["package_id"])
    return sorted({str(value) for value in values if value})


def _nlp_row(spec: Mapping[str, Any], callable_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matches = [row for row in callable_rows
               if row.get("cap") == spec["cap"]
               and _selector_matches(row.get("selector"), spec["selector"])]
    if not matches:
        return {**spec, "status": "unresolved", "model": "", "package_ids": [],
                "present_on": [], "loaded_on": [], "gaps": ["callable_binding_missing"]}
    row = matches[0]
    packages = _packages(row)
    present = sorted({str(node) for node in row.get("present_on") or []})
    loaded = sorted({str(node) for node in row.get("loaded_on") or []})
    gaps = []
    if not packages:
        gaps.append("portable_package_unbound")
    if not present:
        gaps.append("not_present_on_any_node")
    return {**spec, "binding_id": row.get("id") or "", "model": row.get("model") or "",
            "package_id": packages[0] if len(packages) == 1 else "", "package_ids": packages,
            "present_on": present, "loaded_on": loaded,
            "status": "verified" if packages and present else ("observed" if present else "unavailable"),
            "gaps": gaps}


def _role_rows(profile: Mapping[str, Any], callable_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    llms = [row for row in callable_rows if row.get("cap") == "llm.generate"]
    for role, rule in sorted((profile.get("roles") or {}).items()):
        rule = rule if isinstance(rule, Mapping) else {}
        model = str(rule.get("model") or "")
        match = next((row for row in llms if row.get("model") == model), None)
        present = sorted({str(node) for node in (match or {}).get("present_on") or []})
        loaded = sorted({str(node) for node in (match or {}).get("loaded_on") or []})
        gaps = []
        if not model:
            gaps.append("route_model_missing")
        if model and not match:
            gaps.append("callable_provider_missing")
        elif model and not present:
            gaps.append("not_present_on_any_node")
        rows.append({"consumer_id": f"intel.role.{role}", "role": role,
                     "cap": "llm.generate", "model": model, "pin": rule.get("pin") or "",
                     "job_type": rule.get("job_type") or "", "deny_gpu": bool(rule.get("deny_gpu")),
                     "present_on": present, "loaded_on": loaded,
                     "status": "observed" if present else "unavailable", "gaps": gaps})
    return rows


def project(callable_rows: Sequence[Mapping[str, Any]], profile: Mapping[str, Any],
            *, sources: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Join declared Intel consumers to current callable and placement evidence."""
    nlp = [_nlp_row(spec, callable_rows) for spec in NLP_CONSUMERS]
    roles = _role_rows(profile, callable_rows)
    gaps = sorted({gap for row in nlp + roles for gap in row.get("gaps") or []})
    return {"schema": SCHEMA, "profile": profile.get("name") or "intel", "nlp": nlp,
            "roles": roles, "counts": {"nlp": len(nlp), "roles": len(roles),
                                        "ready": sum(row["status"] in {"verified", "observed"}
                                                     for row in nlp + roles)},
            "gaps": gaps, "sources": dict(sources or {})}
