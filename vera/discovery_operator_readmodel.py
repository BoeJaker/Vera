"""Bounded, payload-free operator read models for discovery evidence."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import re
from threading import RLock
from typing import Any, Mapping

# Dual-spelled: in the running app the package is only importable as Vera.vera (the plain 'vera' is a test-path
# spelling), and a plain-only import here took operator_web_capabilities down with it on prod (2026-09-30).
try:  # the test path's spelling first; the app has only Vera.vera, so it takes the fallback - one spelling per process
    from vera.discovery_benchmark import SCHEMA as BENCHMARK_SCHEMA
except ImportError:  # pragma: no cover - the running app
    from Vera.vera.discovery_benchmark import SCHEMA as BENCHMARK_SCHEMA
try:
    from vera.research.discovery_shadow import SHADOW_SCHEMA
except ImportError:  # pragma: no cover - the running app
    from Vera.vera.research.discovery_shadow import SHADOW_SCHEMA


READMODEL_SCHEMA = "vera.discovery-operator-readmodel/v1"
MAX_ENTRIES = 50


def _bounded(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must be present and bounded")
    return value


def _metrics(value: Any, name: str) -> dict[str, int | float | None]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise ValueError(f"{name} must be a bounded metric object")
    result: dict[str, int | float | None] = {}
    for key, metric in value.items():
        key = _bounded(key, f"{name} key")
        if metric is not None and (isinstance(metric, bool) or
                                   not isinstance(metric, (int, float)) or
                                   not math.isfinite(metric)):
            raise ValueError(f"{name} values must be finite numbers or null")
        result[key] = metric
    return result


def project_route_report(report: Any) -> dict[str, Any]:
    # Use the protocol shape rather than isinstance: Vera can load this module
    # through either the ``vera`` or ``Vera.vera`` package spelling. Requiring
    # one class identity would split the read model at that compatibility seam.
    try:
        result, plan = report.result, report.execution_plan
        ranked_options = report.ranked_options
        failures = report.failures
        rejected_option_ids = report.rejected_option_ids
    except AttributeError as exc:
        raise ValueError("report must implement the discovery route contract") from exc
    candidate_options = {
        option.option_id: (candidate, option)
        for candidate in result.candidates for option in candidate.options
    }
    ranked = {value.option_id: value for value in ranked_options}
    selections: list[dict[str, Any]] = []
    for assignment in plan.assignments:
        selected = candidate_options.get(assignment.option_id)
        ranking = ranked.get(assignment.option_id)
        if selected is None or ranking is None:
            raise ValueError("route selection evidence is incomplete")
        candidate, option = selected
        selections.append({
            "candidate_id": assignment.candidate_id,
            "source_id": candidate.source_id,
            "source_kind": candidate.source_kind,
            "method": option.method,
            "provider": option.provider,
            "provider_revision": option.provider_revision,
            "resource": assignment.resource,
            "output_kinds": list(option.output_kinds),
            "rank": ranking.rank,
            "score": ranking.score,
            "worker_id": assignment.worker_id,
            "worker_evidence_id": assignment.worker_evidence_id,
            "gpu_admission_id": assignment.gpu_admission_id,
        })
    return {
        "kind": "route", "request_id": result.request.request_id,
        "result_id": result.result_id, "plan_id": plan.plan_id,
        "evidence_scope": "explicit_route_execution",
        "execution_authority": "discovery.route",
        "consumer_routing_changed": False,
        "control_authority": False,
        "counts": {
            "candidates": len(result.candidates), "receipts": len(result.receipts),
            "context": len(result.context), "datasets": len(result.datasets),
            "artifacts": len(result.artifacts), "failures": len(failures),
            "rejected_options": len(rejected_option_ids),
        },
        "ranked_options": [{
            "candidate_id": value.candidate_id, "source_id": value.source_id,
            "option_id": value.option_id, "rank": value.rank, "score": value.score,
        } for value in ranked_options],
        "selections": selections,
        "assignments": [{
            "candidate_id": value.candidate_id, "option_id": value.option_id,
            "worker_id": value.worker_id, "resource": value.resource,
            "worker_evidence_id": value.worker_evidence_id,
            "gpu_admission_id": value.gpu_admission_id,
        } for value in plan.assignments],
        "receipts": [{
            "receipt_id": value.receipt_id, "candidate_id": value.candidate_id,
            "option_id": value.option_id, "provider_revision": value.provider_revision,
            "status": value.status, "item_count": value.item_count,
            "byte_count": value.byte_count, "duration_ms": value.duration_ms,
            "cost_units": value.cost_units, "error_code": value.error_code,
        } for value in result.receipts],
        "failures": [{
            "stage": value.stage, "participant": value.participant,
            "reason": value.reason, "candidate_id": value.candidate_id,
            "option_id": value.option_id,
        } for value in failures],
    }


def project_benchmark_comparison(
        comparison: Mapping[str, Any], *,
        evidence_scope: str = "offline_comparison",
        production_ready: bool = False,
        authority_changed: bool = False,
        providers_invoked: bool | None = None) -> dict[str, Any]:
    if not isinstance(comparison, Mapping) or comparison.get("schema") != BENCHMARK_SCHEMA:
        raise ValueError("comparison must be a context benchmark result")
    required = ("fixture_id", "snapshot_id", "baseline_variant_id",
                "candidate_variant_id", "summaries", "blockers", "passed")
    if any(name not in comparison for name in required):
        raise ValueError("comparison is incomplete")
    summaries = comparison["summaries"]
    if not isinstance(summaries, Mapping):
        raise ValueError("comparison summaries must be an object")
    baseline = _bounded(comparison["baseline_variant_id"], "baseline variant")
    candidate = _bounded(comparison["candidate_variant_id"], "candidate variant")
    if baseline not in summaries or candidate not in summaries:
        raise ValueError("comparison variants are missing summaries")
    blockers = comparison["blockers"]
    if not isinstance(blockers, list) or len(blockers) > 256:
        raise ValueError("comparison blockers are invalid")
    try:
        blockers = [_bounded(value, "comparison blocker") for value in blockers]
    except ValueError as exc:
        raise ValueError("comparison blockers are invalid") from exc
    observation_ids = comparison.get("observation_ids") or ()
    if not isinstance(observation_ids, (list, tuple)) or len(observation_ids) > 100_000:
        raise ValueError("comparison observation ids are invalid")
    try:
        tuple(_bounded(value, "observation id") for value in observation_ids)
    except ValueError as exc:
        raise ValueError("comparison observation ids are invalid") from exc
    if not isinstance(comparison["passed"], bool):
        raise ValueError("comparison passed must be boolean")
    evidence_scope = _bounded(evidence_scope, "evidence scope")
    for name, value in (("production_ready", production_ready),
                        ("authority_changed", authority_changed)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be boolean")
    if providers_invoked is not None and not isinstance(providers_invoked, bool):
        raise ValueError("providers_invoked must be boolean or null")
    # Summaries are already aggregate-only; copy only the reviewed metric families.
    def summary(value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("variant summary must be an object")
        scalar_names = ("samples", "completed", "unsuccessful_rate",
                        "useful_context_rate", "policy_violations")
        output = {name: value.get(name) for name in scalar_names}
        if any(item is not None and (isinstance(item, bool) or
               not isinstance(item, (int, float)) or not math.isfinite(item))
               for item in output.values()):
            raise ValueError("variant summary scalars must be finite numbers or null")
        output.update({name: _metrics(value.get(name), name)
                       for name in ("quality", "latency_ms", "resources")})
        return output
    return {
        "kind": "benchmark", "fixture_id": _bounded(comparison["fixture_id"], "fixture"),
        "snapshot_id": _bounded(comparison["snapshot_id"], "snapshot"),
        "baseline_variant_id": baseline, "candidate_variant_id": candidate,
        "summaries": {baseline: summary(summaries[baseline]),
                      candidate: summary(summaries[candidate])},
        "blockers": blockers, "passed": comparison["passed"],
        "observation_count": len(observation_ids),
        "evidence_scope": evidence_scope,
        "production_ready": production_ready,
        "authority_changed": authority_changed,
        "providers_invoked": providers_invoked,
        "control_authority": False,
    }


def project_research_shadow(observation: Any) -> dict[str, Any]:
    value = observation.to_dict() if hasattr(observation, "to_dict") else observation
    if not isinstance(value, Mapping) or value.get("schema") != SHADOW_SCHEMA:
        raise ValueError("research shadow schema is invalid")
    required = {
        "observation_id", "producer", "query_digest", "native_result_count",
        "portable_context_count", "excluded_count", "full_text_count",
        "screenshot_count", "portable_item_ids", "portable_revision_digests",
        "execution_authority", "worker_evidence_available",
        "route_evidence_available", "consumer_routing_changed",
        "duplicate_provider_calls", "payloads_included",
    }
    if not required.issubset(value):
        raise ValueError("research shadow is incomplete")
    counts = {name: value[name] for name in (
        "native_result_count", "portable_context_count", "excluded_count",
        "full_text_count", "screenshot_count")}
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0
           for count in counts.values()):
        raise ValueError("research shadow counts are invalid")
    if counts["portable_context_count"] + counts["excluded_count"] \
            != counts["native_result_count"]:
        raise ValueError("research shadow coverage is inconsistent")
    if counts["full_text_count"] > counts["native_result_count"] \
            or counts["screenshot_count"] > counts["native_result_count"]:
        raise ValueError("research shadow enrichment counts are inconsistent")
    if not isinstance(value["portable_item_ids"], (list, tuple)) \
            or not isinstance(value["portable_revision_digests"], (list, tuple)):
        raise ValueError("research shadow identities must be sequences")
    item_ids = tuple(value["portable_item_ids"])
    revisions = tuple(value["portable_revision_digests"])
    if len(item_ids) != counts["portable_context_count"] or len(revisions) != len(item_ids):
        raise ValueError("research shadow identities are inconsistent")
    if not re.fullmatch(r"rdso_[0-9a-f]{64}", str(value["observation_id"])) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(value["query_digest"])):
        raise ValueError("research shadow digests are invalid")
    if any(not re.fullmatch(r"research_web_[0-9a-f]{32}", str(item))
           for item in item_ids) or any(
               not re.fullmatch(r"sha256:[0-9a-f]{64}", str(revision))
               for revision in revisions):
        raise ValueError("research shadow portable identities are invalid")
    if value["producer"] != "research.gather_web_search" \
            or value["execution_authority"] != "research.native":
        raise ValueError("research shadow authority is invalid")
    flags = ("worker_evidence_available", "route_evidence_available",
             "consumer_routing_changed", "duplicate_provider_calls",
             "payloads_included")
    if any(not isinstance(value[name], bool) for name in flags):
        raise ValueError("research shadow flags must be boolean")
    if any(value[name] for name in flags):
        raise ValueError("research shadow must remain payload-free and non-authoritative")
    return {
        "schema": SHADOW_SCHEMA,
        "kind": "research_shadow",
        "observation_id": _bounded(value["observation_id"], "observation"),
        "producer": _bounded(value["producer"], "producer"),
        "query_digest": _bounded(value["query_digest"], "query digest"),
        **counts,
        "portable_item_ids": [_bounded(item, "portable item") for item in item_ids],
        "portable_revision_digests": [
            _bounded(revision, "portable revision") for revision in revisions],
        "execution_authority": _bounded(value["execution_authority"], "authority"),
        "worker_evidence_available": False,
        "route_evidence_available": False,
        "consumer_routing_changed": False,
        "duplicate_provider_calls": False,
        "payloads_included": False,
    }


class DiscoveryOperatorLedger:
    def __init__(self, capacity: int = MAX_ENTRIES):
        if isinstance(capacity, bool) or not isinstance(capacity, int) \
                or not 1 <= capacity <= MAX_ENTRIES:
            raise ValueError(f"capacity must be between 1 and {MAX_ENTRIES}")
        self._routes: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._benchmarks: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._research_shadows: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = RLock()

    def record_route(self, report: Any) -> dict[str, Any]:
        value = project_route_report(report)
        with self._lock:
            self._routes.append(value)
        return deepcopy(value)

    def record_benchmark(
            self, comparison: Mapping[str, Any], *,
            evidence_scope: str = "offline_comparison",
            production_ready: bool = False,
            authority_changed: bool = False,
            providers_invoked: bool | None = None) -> dict[str, Any]:
        value = project_benchmark_comparison(
            comparison, evidence_scope=evidence_scope,
            production_ready=production_ready,
            authority_changed=authority_changed,
            providers_invoked=providers_invoked)
        with self._lock:
            self._benchmarks.append(value)
        return deepcopy(value)

    def record_research_shadow(self, observation: Any) -> dict[str, Any]:
        value = project_research_shadow(observation)
        with self._lock:
            self._research_shadows.append(value)
        return deepcopy(value)

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ENTRIES:
            raise ValueError(f"limit must be between 1 and {MAX_ENTRIES}")
        with self._lock:
            routes = list(self._routes)[-limit:][::-1]
            benchmarks = list(self._benchmarks)[-limit:][::-1]
            research_shadows = list(self._research_shadows)[-limit:][::-1]
            counts = {"routes": len(self._routes),
                      "benchmarks": len(self._benchmarks),
                      "research_shadows": len(self._research_shadows)}
        return {"schema": READMODEL_SCHEMA, "routes": deepcopy(routes),
                "benchmarks": deepcopy(benchmarks),
                "research_shadows": deepcopy(research_shadows),
                "counts": counts,
                "payloads_included": False, "control_authority": False}


DISCOVERY_OPERATOR_LEDGER = DiscoveryOperatorLedger()
