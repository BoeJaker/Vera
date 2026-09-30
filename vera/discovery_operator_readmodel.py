"""Bounded, payload-free operator read models for discovery evidence."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
from threading import RLock
from typing import Any, Mapping

from vera.discovery_benchmark import SCHEMA as BENCHMARK_SCHEMA


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
    return {
        "kind": "route", "request_id": result.request.request_id,
        "result_id": result.result_id, "plan_id": plan.plan_id,
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


def project_benchmark_comparison(comparison: Mapping[str, Any]) -> dict[str, Any]:
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
    }


class DiscoveryOperatorLedger:
    def __init__(self, capacity: int = MAX_ENTRIES):
        if isinstance(capacity, bool) or not isinstance(capacity, int) \
                or not 1 <= capacity <= MAX_ENTRIES:
            raise ValueError(f"capacity must be between 1 and {MAX_ENTRIES}")
        self._routes: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._benchmarks: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = RLock()

    def record_route(self, report: Any) -> dict[str, Any]:
        value = project_route_report(report)
        with self._lock:
            self._routes.append(value)
        return deepcopy(value)

    def record_benchmark(self, comparison: Mapping[str, Any]) -> dict[str, Any]:
        value = project_benchmark_comparison(comparison)
        with self._lock:
            self._benchmarks.append(value)
        return deepcopy(value)

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ENTRIES:
            raise ValueError(f"limit must be between 1 and {MAX_ENTRIES}")
        with self._lock:
            routes = list(self._routes)[-limit:][::-1]
            benchmarks = list(self._benchmarks)[-limit:][::-1]
            counts = {"routes": len(self._routes),
                      "benchmarks": len(self._benchmarks)}
        return {"schema": READMODEL_SCHEMA, "routes": deepcopy(routes),
                "benchmarks": deepcopy(benchmarks),
                "counts": counts,
                "payloads_included": False, "control_authority": False}


DISCOVERY_OPERATOR_LEDGER = DiscoveryOperatorLedger()
