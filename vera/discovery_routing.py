"""Deterministic execution planning for portable discovery work.

Inputs are bounded evidence supplied by the caller. This module does not poll
workers, inspect a GPU, acquire a lease, enqueue work, or read wall-clock time.
A GPU assignment requires a current gate admission for the exact request,
option and worker; worker health alone is never treated as permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Sequence

from vera.discovery_contract import (
    CollectionOption, DiscoveryRequest, SourceCandidate,
)


WORKER_OFFER_SCHEMA = "vera.discovery-worker-offer/v1"
GPU_ADMISSION_SCHEMA = "vera.discovery-gpu-admission/v1"
EXECUTION_PLAN_SCHEMA = "vera.discovery-execution-plan/v1"
MAX_OFFER_VALIDITY_MS = 60_000
MAX_PLAN_ITEMS = 256
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HASH = re.compile(r"^[a-z]+_[0-9a-f]{64}$")
_RESOURCES = frozenset({"cpu", "gpu", "network", "storage"})
_METHODS = frozenset({
    "api", "capability", "crawl", "dataset_query", "feed", "file_read",
    "repository_fetch", "sitemap",
})
_ADMISSION_STATES = frozenset({"denied", "granted"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, field_name: str, *, optional: bool = False) -> str:
    value = str(value or "").strip()
    if optional and not value:
        return ""
    if not _ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _count(value: Any, field_name: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} must be a bounded non-negative integer")
    return value


def _millisecond(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative Unix millisecond")
    return value


@dataclass(frozen=True, slots=True)
class WorkerProviderBinding:
    provider: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _identifier(self.provider, "provider"))
        object.__setattr__(self, "revision", _identifier(self.revision, "revision"))

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class DiscoveryWorkerOffer:
    worker_id: str
    evidence_source: str
    observed_at_ms: int
    valid_until_ms: int
    resources: tuple[str, ...]
    methods: tuple[str, ...]
    bindings: tuple[WorkerProviderBinding, ...]
    concurrency_limit: int
    in_flight: int
    revision: int = 1
    evidence_id: str = field(init=False)
    schema: str = WORKER_OFFER_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", _identifier(self.worker_id, "worker_id"))
        object.__setattr__(self, "evidence_source", _identifier(
            self.evidence_source, "evidence_source"))
        observed = _millisecond(self.observed_at_ms, "observed_at_ms")
        valid = _millisecond(self.valid_until_ms, "valid_until_ms")
        if valid < observed or valid - observed > MAX_OFFER_VALIDITY_MS:
            raise ValueError("worker offer validity must be ordered and at most 60 seconds")
        resources = tuple(sorted({_identifier(value, "resource")
                                  for value in self.resources}))
        methods = tuple(sorted({_identifier(value, "method") for value in self.methods}))
        if not resources or any(value not in _RESOURCES for value in resources):
            raise ValueError("worker offer resources are unsupported")
        if not methods or any(value not in _METHODS for value in methods):
            raise ValueError("worker offer methods are unsupported")
        try:
            bindings = tuple(sorted(tuple(self.bindings),
                                    key=lambda value: (value.provider, value.revision)))
        except (TypeError, AttributeError) as exc:
            raise ValueError("worker bindings must be a sequence") from exc
        if (not bindings or not all(isinstance(value, WorkerProviderBinding)
                                    for value in bindings) or
                len(set(bindings)) != len(bindings)):
            raise ValueError("worker bindings must be non-empty and unique")
        limit = _count(self.concurrency_limit, "concurrency_limit")
        in_flight = _count(self.in_flight, "in_flight")
        if limit < 1 or in_flight > limit:
            raise ValueError("worker load exceeds its concurrency limit")
        revision = _count(self.revision, "revision")
        if revision < 1:
            raise ValueError("worker offer revision must be positive")
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "methods", methods)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "concurrency_limit", limit)
        object.__setattr__(self, "in_flight", in_flight)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "evidence_id", _digest("dwo_", self.identity_dict()))

    @property
    def available_slots(self) -> int:
        return self.concurrency_limit - self.in_flight

    def is_current(self, as_of_ms: int) -> bool:
        as_of_ms = _millisecond(as_of_ms, "as_of_ms")
        return self.observed_at_ms <= as_of_ms <= self.valid_until_ms

    def supports(self, option: CollectionOption) -> bool:
        return (option.resource in self.resources and option.method in self.methods and
                (not option.network_required or "network" in self.resources) and
                WorkerProviderBinding(option.provider,
                                      option.provider_revision) in self.bindings)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "worker_id": self.worker_id,
            "evidence_source": self.evidence_source,
            "observed_at_ms": self.observed_at_ms,
            "valid_until_ms": self.valid_until_ms,
            "resources": list(self.resources), "methods": list(self.methods),
            "bindings": [value.to_dict() for value in self.bindings],
            "concurrency_limit": self.concurrency_limit,
            "in_flight": self.in_flight, "revision": self.revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id,
                "available_slots": self.available_slots, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class GpuAdmissionReceipt:
    request_id: str
    option_id: str
    worker_id: str
    gate_id: str
    state: str
    observed_at_ms: int
    valid_until_ms: int
    lease_id: str = ""
    holder: str = ""
    reason: str = ""
    admission_id: str = field(init=False)
    schema: str = GPU_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        if not re.fullmatch(r"dsr_[0-9a-f]{64}", str(self.request_id or "")):
            raise ValueError("invalid request_id")
        if not re.fullmatch(r"dco_[0-9a-f]{64}", str(self.option_id or "")):
            raise ValueError("invalid option_id")
        for name in ("worker_id", "gate_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        state = _identifier(self.state, "state")
        if state not in _ADMISSION_STATES:
            raise ValueError("unsupported GPU admission state")
        object.__setattr__(self, "state", state)
        observed = _millisecond(self.observed_at_ms, "observed_at_ms")
        valid = _millisecond(self.valid_until_ms, "valid_until_ms")
        if valid < observed or valid - observed > MAX_OFFER_VALIDITY_MS:
            raise ValueError("GPU admission validity must be ordered and at most 60 seconds")
        lease = _identifier(self.lease_id, "lease_id", optional=True)
        holder = _identifier(self.holder, "holder", optional=True)
        reason = _identifier(self.reason, "reason", optional=True)
        if state == "granted" and (not lease or holder or reason):
            raise ValueError("granted GPU admission requires only a lease_id")
        if state == "denied" and (lease or not reason):
            raise ValueError("denied GPU admission requires a reason and no lease")
        object.__setattr__(self, "lease_id", lease)
        object.__setattr__(self, "holder", holder)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "admission_id", _digest(
            "dga_", self.identity_dict()))

    def is_current(self, as_of_ms: int) -> bool:
        as_of_ms = _millisecond(as_of_ms, "as_of_ms")
        return self.observed_at_ms <= as_of_ms <= self.valid_until_ms

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request_id,
            "option_id": self.option_id, "worker_id": self.worker_id,
            "gate_id": self.gate_id, "state": self.state,
            "observed_at_ms": self.observed_at_ms,
            "valid_until_ms": self.valid_until_ms, "lease_id": self.lease_id,
            "holder": self.holder, "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"admission_id": self.admission_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class DiscoveryAssignment:
    candidate_id: str
    option_id: str
    worker_id: str
    worker_evidence_id: str
    resource: str
    gpu_admission_id: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"dsc_[0-9a-f]{64}", str(self.candidate_id or "")):
            raise ValueError("invalid candidate_id")
        if not re.fullmatch(r"dco_[0-9a-f]{64}", str(self.option_id or "")):
            raise ValueError("invalid option_id")
        object.__setattr__(self, "worker_id", _identifier(self.worker_id, "worker_id"))
        if not re.fullmatch(r"dwo_[0-9a-f]{64}", str(self.worker_evidence_id or "")):
            raise ValueError("invalid worker_evidence_id")
        if self.resource not in _RESOURCES:
            raise ValueError("unsupported assignment resource")
        if self.resource == "gpu":
            if not re.fullmatch(r"dga_[0-9a-f]{64}", self.gpu_admission_id):
                raise ValueError("GPU assignment requires admission identity")
        elif self.gpu_admission_id:
            raise ValueError("non-GPU assignment cannot claim GPU admission")


@dataclass(frozen=True, slots=True)
class DiscoveryRefusal:
    candidate_id: str
    option_id: str
    reason: str
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"dsc_[0-9a-f]{64}", str(self.candidate_id or "")):
            raise ValueError("invalid candidate_id")
        if not re.fullmatch(r"dco_[0-9a-f]{64}", str(self.option_id or "")):
            raise ValueError("invalid option_id")
        object.__setattr__(self, "reason", _identifier(self.reason, "reason"))
        if self.evidence_id and not _HASH.fullmatch(self.evidence_id):
            raise ValueError("invalid refusal evidence_id")


@dataclass(frozen=True, slots=True)
class DiscoveryExecutionPlan:
    request_id: str
    as_of_ms: int
    assignments: tuple[DiscoveryAssignment, ...]
    refusals: tuple[DiscoveryRefusal, ...]
    plan_id: str = field(init=False)
    schema: str = EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if not re.fullmatch(r"dsr_[0-9a-f]{64}", str(self.request_id or "")):
            raise ValueError("invalid request_id")
        _millisecond(self.as_of_ms, "as_of_ms")
        if (len(self.assignments) + len(self.refusals) > MAX_PLAN_ITEMS or
                not all(isinstance(value, DiscoveryAssignment)
                        for value in self.assignments) or
                not all(isinstance(value, DiscoveryRefusal)
                        for value in self.refusals)):
            raise ValueError("plan decisions are invalid or exceed the limit")
        decisions = [value.option_id for value in (*self.assignments, *self.refusals)]
        if len(decisions) != len(set(decisions)):
            raise ValueError("each option must have exactly one decision")
        object.__setattr__(self, "plan_id", _digest("dep_", self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request_id,
            "as_of_ms": self.as_of_ms,
            "assignments": [{
                "candidate_id": value.candidate_id,
                "option_id": value.option_id, "worker_id": value.worker_id,
                "worker_evidence_id": value.worker_evidence_id,
                "resource": value.resource,
                "gpu_admission_id": value.gpu_admission_id,
            } for value in self.assignments],
            "refusals": [{
                "candidate_id": value.candidate_id,
                "option_id": value.option_id, "reason": value.reason,
                "evidence_id": value.evidence_id,
            } for value in self.refusals],
        }


def plan_discovery_execution(
    request: DiscoveryRequest,
    candidates: Sequence[SourceCandidate],
    selected_option_ids: Sequence[str],
    workers: Sequence[DiscoveryWorkerOffer],
    gpu_admissions: Sequence[GpuAdmissionReceipt],
    *,
    as_of_ms: int,
) -> DiscoveryExecutionPlan:
    """Plan selected options without executing or inferring resource state."""
    if not isinstance(request, DiscoveryRequest):
        raise ValueError("request must be DiscoveryRequest")
    as_of_ms = _millisecond(as_of_ms, "as_of_ms")
    candidates = tuple(candidates)
    if (len(candidates) > MAX_PLAN_ITEMS or
            not all(isinstance(value, SourceCandidate) for value in candidates)):
        raise ValueError("candidates are invalid or exceed the plan limit")
    if any(value.request_id != request.request_id for value in candidates):
        raise ValueError("candidate belongs to another request")
    candidate_ids = [value.candidate_id for value in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate identities must be unique")
    option_index = {
        option.option_id: (candidate, option)
        for candidate in candidates for option in candidate.options
    }
    selected = tuple(selected_option_ids)
    if (len(selected) > request.max_sources or len(selected) != len(set(selected)) or
            any(value not in option_index for value in selected)):
        raise ValueError("selected options must be unique, known, and within source budget")
    selected_candidates = [option_index[value][0].candidate_id for value in selected]
    if len(selected_candidates) != len(set(selected_candidates)):
        raise ValueError("only one collection option may be selected per candidate")
    workers = tuple(workers)
    if (len(workers) > MAX_PLAN_ITEMS or
            not all(isinstance(value, DiscoveryWorkerOffer) for value in workers)):
        raise ValueError("worker offers are invalid or exceed the plan limit")
    worker_ids = [value.worker_id for value in workers]
    if len(worker_ids) != len(set(worker_ids)):
        raise ValueError("worker offer identities must be unique")
    admissions = tuple(gpu_admissions)
    if not all(isinstance(value, GpuAdmissionReceipt) for value in admissions):
        raise ValueError("GPU admissions are invalid")
    admission_keys = [(value.option_id, value.worker_id) for value in admissions]
    if len(admission_keys) != len(set(admission_keys)):
        raise ValueError("GPU admissions must be unique per option and worker")
    admission_index = {key: value for key, value in zip(admission_keys, admissions)}

    remaining = {value.worker_id: value.available_slots for value in workers}
    assignments: list[DiscoveryAssignment] = []
    refusals: list[DiscoveryRefusal] = []
    spent_cost = 0
    reserved_bytes = 0
    ordered = sorted(selected, key=lambda option_id: (
        -option_index[option_id][0].relevance_score,
        -option_index[option_id][0].authority_score,
        option_index[option_id][1].estimated_latency_ms,
        option_id))
    for option_id in ordered:
        candidate, option = option_index[option_id]
        if spent_cost + option.estimated_cost_units > request.max_cost_units:
            refusals.append(DiscoveryRefusal(
                candidate.candidate_id, option_id, "cost_budget_exceeded"))
            continue
        if reserved_bytes + option.max_bytes > request.max_bytes:
            refusals.append(DiscoveryRefusal(
                candidate.candidate_id, option_id, "byte_budget_exceeded"))
            continue
        current = [value for value in workers if value.is_current(as_of_ms)
                   and remaining[value.worker_id] > 0 and value.supports(option)]
        if not current:
            refusals.append(DiscoveryRefusal(
                candidate.candidate_id, option_id, "no_current_worker_offer"))
            continue
        current.sort(key=lambda value: (
            (value.concurrency_limit - remaining[value.worker_id]) /
            value.concurrency_limit,
            -remaining[value.worker_id], value.worker_id))
        chosen = None
        admission = None
        if option.resource == "gpu":
            denied = None
            for worker in current:
                proof = admission_index.get((option_id, worker.worker_id))
                if proof is None or proof.request_id != request.request_id:
                    continue
                if not proof.is_current(as_of_ms):
                    denied = proof
                    continue
                if proof.state == "granted":
                    chosen, admission = worker, proof
                    break
                denied = proof
            if chosen is None:
                refusals.append(DiscoveryRefusal(
                    candidate.candidate_id, option_id,
                    "gpu_gate_denied" if denied and denied.state == "denied"
                    else "gpu_admission_missing_or_stale",
                    denied.admission_id if denied else ""))
                continue
        else:
            chosen = current[0]
        remaining[chosen.worker_id] -= 1
        spent_cost += option.estimated_cost_units
        reserved_bytes += option.max_bytes
        assignments.append(DiscoveryAssignment(
            candidate.candidate_id, option_id, chosen.worker_id,
            chosen.evidence_id, option.resource,
            admission.admission_id if admission else ""))
    assignments.sort(key=lambda value: value.option_id)
    refusals.sort(key=lambda value: value.option_id)
    return DiscoveryExecutionPlan(
        request.request_id, as_of_ms, tuple(assignments), tuple(refusals))
