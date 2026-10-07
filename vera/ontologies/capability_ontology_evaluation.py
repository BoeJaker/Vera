"""Deterministic comparison harness for capability-ontology routing hints.

This module evaluates, but never activates, ontology influence. Only a typed
``preferred_over`` edge can become a resolver preference. Caller preferences
remain authoritative, eligibility remains owned by the resolver, and malformed,
unknown, or cyclic relation sets fail closed to the unmodified request.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
import time
from typing import Any

# Dual-spelled: the running app has only Vera.vera (eval.ontology.decision answered "No module named 'vera'" on prod,
# 2026-09-30); the test path's spelling first, so one process never holds both.
try:
    from vera.capability_contract_core import project_registry
    from vera.capability_resolver_core import resolve_shadow
except ImportError:  # pragma: no cover - the running app
    from Vera.vera.capability_contract_core import project_registry
    from Vera.vera.capability_resolver_core import resolve_shadow


ONTOLOGY_DECISION_CORPUS_SCHEMA = "vera.capability-ontology-decision-corpus/v1"
ONTOLOGY_DECISION_REPORT_SCHEMA = "vera.capability-ontology-decision-report/v1"
VARIANTS = ("baseline", "curated", "generated")
MAX_CASES = 200
MAX_RELATIONS = 500
MAX_TIMING_REPETITIONS = 200


def load_ontology_decision_corpus(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("ontology decision corpus root must be an object")
    return value


def _name(value: Any, field: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 256:
        raise ValueError(f"{field} must be a bounded identifier")
    return value


def _relations(case: Mapping[str, Any], variant: str) -> tuple[Mapping[str, Any], ...]:
    if variant == "baseline":
        return ()
    value = case.get(f"{variant}_relations", [])
    if not isinstance(value, list) or len(value) > MAX_RELATIONS:
        raise ValueError(f"{variant} relations must be a bounded list")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{variant} relations must contain objects")
    return tuple(value)


def validate_ontology_decision_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if corpus.get("schema") != ONTOLOGY_DECISION_CORPUS_SCHEMA:
        issues.append({"code": "schema.invalid", "path": "schema"})
    policy = corpus.get("policy")
    required = {"synthetic_only": True, "model_calls": False,
                "network_calls": False, "capability_execution": False,
                "ontology_activation": False}
    if not isinstance(policy, Mapping) or any(
            policy.get(key) is not value for key, value in required.items()):
        issues.append({"code": "policy.non_authoritative_required", "path": "policy"})
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        issues.append({"code": "cases.invalid", "path": "cases"})
        cases = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases.{index}"
        if not isinstance(case, Mapping):
            issues.append({"code": "case.invalid", "path": path})
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen:
            issues.append({"code": "case.id_invalid", "path": f"{path}.id"})
        seen.add(case_id)
        registry = case.get("registry")
        if not isinstance(registry, Mapping) or not registry:
            issues.append({"code": "case.registry_required", "path": f"{path}.registry"})
            registry = {}
        if not isinstance(case.get("request"), Mapping):
            issues.append({"code": "case.request_required", "path": f"{path}.request"})
        expected = case.get("expected_selected")
        if expected is not None and expected not in registry:
            issues.append({"code": "case.expected_unknown", "path": f"{path}.expected_selected"})
        unsafe = case.get("unsafe_names", [])
        if not isinstance(unsafe, list) or any(name not in registry for name in unsafe):
            issues.append({"code": "case.unsafe_names_invalid", "path": f"{path}.unsafe_names"})
        for variant in ("curated", "generated"):
            try:
                _relations(case, variant)
            except ValueError:
                issues.append({"code": "case.relations_invalid",
                               "path": f"{path}.{variant}_relations"})
    return {"schema": ONTOLOGY_DECISION_REPORT_SCHEMA, "ok": not issues,
            "issues": issues, "case_count": len(cases)}


def _ontology_preference(
        registry: Mapping[str, Any], request: Mapping[str, Any],
        relations: Sequence[Mapping[str, Any]], provenance: str,
        ) -> tuple[tuple[str, ...], str, tuple[dict[str, Any], ...]]:
    caller_preferred = request.get("preferred") or []
    if caller_preferred:
        return (), "caller_preference_authoritative", ()
    task = str(request.get("canonical_task") or "").strip()
    family = {
        str(name) for name, record in registry.items()
        if isinstance(record, Mapping)
        and str((record.get("contract") or {}).get("canonical_task") or "").strip() == task
    }
    edges: list[tuple[str, str]] = []
    normalized: list[dict[str, Any]] = []
    for relation in relations:
        source = _name(relation.get("from"), "relation from")
        target = _name(relation.get("to"), "relation to")
        relation_type = str(relation.get("relation") or "").strip()
        relation_provenance = str(relation.get("provenance") or "").strip()
        confidence = relation.get("confidence")
        if relation_provenance != provenance:
            raise ValueError("relation provenance does not match its variant")
        if relation_type != "preferred_over":
            raise ValueError("only preferred_over relations are evaluable")
        if source == target or source not in family or target not in family:
            raise ValueError("ontology relation must connect distinct candidates in one task family")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("relation confidence must be between zero and one")
        if confidence < .5:
            continue
        edge = (source, target)
        if edge not in edges:
            edges.append(edge)
            normalized.append({"from": source, "to": target,
                               "relation": relation_type,
                               "confidence": float(confidence),
                               "provenance": provenance})
    if not edges:
        return (), "no_applicable_relations", tuple(normalized)

    nodes = sorted({node for edge in edges for node in edge})
    incoming = {node: 0 for node in nodes}
    outgoing = {node: [] for node in nodes}
    for source, target in edges:
        outgoing[source].append(target)
        incoming[target] += 1
    ready = sorted(node for node, count in incoming.items() if count == 0)
    ordered: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for target in sorted(outgoing[node]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if len(ordered) != len(nodes):
        raise ValueError("ontology preference relations contain a cycle")
    return tuple(ordered), "applied", tuple(normalized)


def _semantic_rank(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    rank = candidate.get("rank") or {}
    return (rank.get("preference"), rank.get("reliability"), rank.get("quality"),
            rank.get("latency_p95_ms"), rank.get("cost_normalized_per_call"),
            rank.get("load_utilization"), rank.get("local"))


def _run_variant(case: Mapping[str, Any], variant: str) -> dict[str, Any]:
    registry = case["registry"]
    request = dict(case["request"])
    relations = _relations(case, variant)
    applied: tuple[dict[str, Any], ...] = ()
    hint_status = "baseline"
    invalid_reason = ""
    if variant != "baseline":
        try:
            preference, hint_status, applied = _ontology_preference(
                registry, request, relations, variant)
            if preference:
                request["preferred"] = list(preference)
        except ValueError as exc:
            hint_status = "invalid"
            invalid_reason = str(exc)
    manifests = project_registry(registry)
    resolution = resolve_shadow(
        manifests, request, observations=case.get("observations") or [])
    selected = resolution.get("selected")
    eligible = resolution.get("eligible") or []
    ambiguous = len(eligible) > 1 and _semantic_rank(eligible[0]) == _semantic_rank(eligible[1])
    unsafe = selected in set(case.get("unsafe_names") or [])
    expected = case.get("expected_selected")
    exact = selected == expected
    hint_bytes = len(json.dumps(
        {"preferred": request.get("preferred") or [], "relations": applied},
        sort_keys=True, separators=(",", ":")).encode("utf-8")) if applied else 0
    helpful = sum(edge["from"] == expected and edge["to"] != expected for edge in applied)
    harmful = sum(edge["to"] == expected or edge["from"] in set(
        case.get("unsafe_names") or []) for edge in applied)
    return {
        "id": case["id"], "selected": selected, "expected_selected": expected,
        "exact": exact, "unsafe_selected": unsafe, "ambiguous": ambiguous,
        "authorized": resolution.get("authorized"), "executed": resolution.get("executed"),
        "hint_status": hint_status, "invalid_reason": invalid_reason,
        "applied_relations": len(applied), "helpful_relations": helpful,
        "harmful_relations": harmful, "hint_bytes": hint_bytes,
        "hint_estimated_tokens": (hint_bytes + 3) // 4,
    }


def _percentile(values: Sequence[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def evaluate_ontology_decision_corpus(
        corpus: Mapping[str, Any], *, timing_repetitions: int = 20) -> dict[str, Any]:
    """Compare three shadow variants without executing or authorizing a cap."""
    validation = validate_ontology_decision_corpus(corpus)
    if not validation["ok"]:
        return {**validation, "variants": {}, "decision": "invalid_corpus"}
    if isinstance(timing_repetitions, bool) or not isinstance(timing_repetitions, int) \
            or not 1 <= timing_repetitions <= MAX_TIMING_REPETITIONS:
        raise ValueError("timing_repetitions must be between 1 and 200")

    variant_reports: dict[str, Any] = {}
    for variant in VARIANTS:
        cases = [_run_variant(case, variant) for case in corpus["cases"]]
        timings: list[int] = []
        for _ in range(timing_repetitions):
            started = time.perf_counter_ns()
            for case in corpus["cases"]:
                _run_variant(case, variant)
            timings.append((time.perf_counter_ns() - started) // 1000)
        total = len(cases)
        relation_total = sum(case["applied_relations"] for case in cases)
        helpful = sum(case["helpful_relations"] for case in cases)
        harmful = sum(case["harmful_relations"] for case in cases)
        variant_reports[variant] = {
            "selection_accuracy": round(sum(case["exact"] for case in cases) / total, 4),
            "unsafe_choice_rate": round(sum(case["unsafe_selected"] for case in cases) / total, 4),
            "ambiguity_rate": round(sum(case["ambiguous"] for case in cases) / total, 4),
            "relation_precision": (round(helpful / (helpful + harmful), 4)
                                   if helpful + harmful else None),
            "invalid_relation_cases": sum(case["hint_status"] == "invalid" for case in cases),
            "applied_relations": relation_total,
            "hint_bytes": sum(case["hint_bytes"] for case in cases),
            "hint_estimated_tokens": sum(case["hint_estimated_tokens"] for case in cases),
            "latency_us": {"status": "observed_local", "repetitions": timing_repetitions,
                           "p50": _percentile(timings, .50),
                           "p95": _percentile(timings, .95)},
            "cases": cases,
        }
    baseline = variant_reports["baseline"]
    generated = variant_reports["generated"]
    generated_passes = (
        generated["selection_accuracy"] > baseline["selection_accuracy"]
        and generated["unsafe_choice_rate"] == 0
        and generated["ambiguity_rate"] <= baseline["ambiguity_rate"]
        and generated["invalid_relation_cases"] == 0
        and generated["relation_precision"] is not None
        and generated["relation_precision"] >= .9)
    return {
        **validation,
        "mode": "shadow",
        "authoritative": False,
        "model_calls": False,
        "capability_execution": False,
        "variants": variant_reports,
        "generated_gate": {
            "requires_accuracy_gain": True,
            "minimum_relation_precision": .9,
            "requires_zero_unsafe_choices": True,
            "requires_zero_invalid_relation_cases": True,
            "passed": generated_passes,
        },
        "decision": ("retain_generated_relations_for_further_review"
                     if generated_passes else "disable_generated_relations"),
    }
