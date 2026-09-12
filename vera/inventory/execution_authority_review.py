"""Deterministic review of scheduler, job, and transition authority surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SCHEMA = "vera.execution-authority-review/v1"
FAMILIES = frozenset({"scheduler", "job", "transition"})
ROLES = frozenset({
    "executor", "driver", "state_gateway", "state_authority",
    "observer", "projection", "native_adapter",
})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")
MAX_SOURCE_BYTES = 4 * 1024 * 1024


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _text(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionAuthoritySurface:
    name: str
    family: str
    role: str
    semantic_scope: str
    state_owner: str
    source_path: str
    symbols: tuple[str, ...]
    executes: bool
    persists: bool
    shared_contract: str = "none"
    migration_target: str = "none"
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        for attr in ("name", "semantic_scope", "state_owner", "shared_contract",
                     "migration_target"):
            object.__setattr__(self, attr, _text(getattr(self, attr), attr))
        if self.family not in FAMILIES:
            raise ValueError("unsupported authority family")
        if self.role not in ROLES:
            raise ValueError("unsupported authority role")
        path = str(self.source_path or "").replace("\\", "/").strip("/")
        if not path.startswith("vera/") or ".." in path.split("/"):
            raise ValueError("source path must stay within vera")
        object.__setattr__(self, "source_path", path)
        symbols = tuple(sorted({str(item or "").strip() for item in self.symbols}))
        if any(not _SYMBOL.fullmatch(item) for item in symbols):
            raise ValueError("source symbol must be a bounded Python identifier")
        if not symbols:
            raise ValueError("authority surface requires source symbols")
        object.__setattr__(self, "symbols", symbols)
        if not isinstance(self.executes, bool) or not isinstance(self.persists, bool):
            raise ValueError("executes and persists must be boolean")
        if self.role in {"observer", "projection"} and self.executes:
            raise ValueError("observer and projection surfaces cannot execute")
        object.__setattr__(self, "surface_id", _identity("eas_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "family": self.family, "role": self.role,
            "semantic_scope": self.semantic_scope, "state_owner": self.state_owner,
            "source_path": self.source_path, "symbols": list(self.symbols),
            "executes": self.executes, "persists": self.persists,
            "shared_contract": self.shared_contract,
            "migration_target": self.migration_target,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    ExecutionAuthoritySurface(
        "core.interval_scheduler", "scheduler", "executor",
        "process_interval_callbacks", "vera.capability_orchestration.SCHEDULED_TASKS",
        "vera/capability_orchestration.py", ("scheduler_loop",), True, False),
    ExecutionAuthoritySurface(
        "calendar.action_scheduler", "scheduler", "executor",
        "calendar_actions", "vera.calendar.action_store",
        "vera/calendar/longterm_scheduler.py", ("_tick",), True, True,
        "vera.workflow-schedule/v1"),
    ExecutionAuthoritySurface(
        "dream.trigger_scheduler", "scheduler", "executor",
        "dream_triggers", "vera.dream.trigger_store",
        "vera/dream/dream_capabilities.py", ("_scheduler_loop",), True, True,
        "vera.workflow-schedule/v1"),
    ExecutionAuthoritySurface(
        "research.iteration_scheduler", "scheduler", "executor",
        "research_iterations", "vera.research.iteration_store",
        "vera/research/researcher_api.py", ("_run_iteration_loop",), True, True,
        "vera.workflow-schedule/v1"),
    ExecutionAuthoritySurface(
        "idle_queue.service", "job", "executor", "deferred_idle_jobs",
        "vera.idle_queue_service", "vera/idle_queue_service.py",
        ("drain_once", "_run_job"), True, True),
    ExecutionAuthoritySurface(
        "idle_queue.service_gateway", "job", "state_gateway",
        "deferred_idle_jobs", "vera.idle_queue_service",
        "vera/idle_queue_service.py", ("load_jobs", "save_job", "drop_job"),
        False, True),
    ExecutionAuthoritySurface(
        "ide.idle_queue_gateway", "job", "state_gateway",
        "deferred_idle_jobs", "vera.idle_queue_service",
        "vera/ide/ide_claude_sessions_capabilities.py",
        ("_idle_jobs", "_idle_put", "_idle_drop"), False, True,
        migration_target="vera.idle_queue_service"),
    ExecutionAuthoritySurface(
        "ide.idle_queue_driver", "job", "driver", "deferred_idle_jobs",
        "vera.idle_queue_service", "vera/ide/ide_claude_sessions_capabilities.py",
        ("_idle_queue_tick",), True, False,
        migration_target="vera.idle_queue_service"),
    ExecutionAuthoritySurface(
        "worker.job_persistence", "job", "state_authority",
        "worker_inference_job_history", "vera.workers.job_persistence",
        "vera/workers/job_persistance.py", ("_persist",), False, True),
    ExecutionAuthoritySurface(
        "workshop.awaited_job_observatory", "job", "observer",
        "agent_awaited_jobs", "vera.dag.workshop_runtime",
        "vera/dag/dag_workshop_capabilities.py", ("_jobs_register",), False, False),
    ExecutionAuthoritySurface(
        "schedule.lifecycle_projection", "transition", "projection",
        "portable_schedule_lifecycle", "native_schedule_owners",
        "vera/execution/workflow_schedule_lifecycle.py",
        ("transition_schedule_lifecycle",), False, False,
        "vera.workflow-schedule-lifecycle/v1"),
    ExecutionAuthoritySurface(
        "calendar.lifecycle_adapter", "transition", "native_adapter",
        "calendar_actions", "vera.calendar.action_store",
        "vera/calendar/longterm_scheduler.py", ("_transition_action_lifecycle",),
        True, True, "vera.workflow-schedule-lifecycle/v1"),
    ExecutionAuthoritySurface(
        "dream.lifecycle_adapter", "transition", "native_adapter",
        "dream_triggers", "vera.dream.trigger_store",
        "vera/dream/dream_capabilities.py", ("_transition_dream_lifecycle",),
        True, True, "vera.workflow-schedule-lifecycle/v1"),
    ExecutionAuthoritySurface(
        "fabric.revision_transition", "transition", "state_authority",
        "fabric_projection_revisions", "vera.fabric.revision_store",
        "vera/fabric/revision_store.py", ("transition",), True, True,
        "vera.projection-revision/v1"),
    ExecutionAuthoritySurface(
        "operator.run_projection_transition", "transition", "projection",
        "operator_run_projection", "vera.run_protocol",
        "vera/operator/operator_run_projection.py", ("_transition",), False, False,
        "vera.run/v1"),
)


def _source_evidence(root: Path, surface: ExecutionAuthoritySurface) -> dict[str, Any]:
    root = root.resolve()
    path = (root / surface.source_path).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"authority source is unavailable: {surface.source_path}")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"authority source is too large: {surface.source_path}")
    text = raw.decode("utf-8")
    missing = [symbol for symbol in surface.symbols
               if not re.search(rf"\b(?:async\s+def|def)\s+{re.escape(symbol)}\s*\(", text)]
    if missing:
        raise ValueError(
            f"authority source symbols are missing from {surface.source_path}: {missing}")
    return {
        "surface_id": surface.surface_id,
        "source_path_digest": "sha256:" + hashlib.sha256(
            surface.source_path.encode()).hexdigest(),
        "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "symbols": list(surface.symbols),
    }


def build_execution_authority_review(
    repo_root: Path, surfaces: Sequence[ExecutionAuthoritySurface] = CURRENT_SURFACES,
) -> dict[str, Any]:
    """Classify structural overlap without executing or mutating any authority."""
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface_id for item in surfaces}) != len(surfaces):
        raise ValueError("authority surfaces must be non-empty and unique")
    evidence = [_source_evidence(Path(repo_root), item) for item in surfaces]
    groups: dict[tuple[str, str, str, str], list[ExecutionAuthoritySurface]] = {}
    for item in surfaces:
        key = (item.family, item.semantic_scope, item.role, item.state_owner)
        groups.setdefault(key, []).append(item)
    overlaps = []
    for key, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        overlaps.append({
            "family": key[0], "semantic_scope": key[1], "role": key[2],
            "state_owner": key[3],
            "surface_ids": sorted(item.surface_id for item in items),
            "migration_targets": sorted({item.migration_target for item in items
                                         if item.migration_target != "none"}),
        })
    present_families = {item.family for item in surfaces}
    recommendations = {}
    for family in sorted(present_families):
        family_overlaps = [item for item in overlaps if item["family"] == family]
        if family == "job" and family_overlaps and all(
                item["migration_targets"] for item in family_overlaps):
            recommendation = "migrate_duplicate_idle_queue_gateways_to_service_owner"
        elif family_overlaps:
            recommendation = "independent_semantic_review_required"
        elif family == "transition" and any(
                item.family == family and item.role == "projection" for item in surfaces):
            recommendation = "retain_native_authority_and_shared_projection_boundary"
        elif family == "scheduler":
            recommendation = "retain_distinct_native_authorities"
        else:
            recommendation = "retain_distinct_authorities"
        recommendations[family] = recommendation
    payload = {
        "schema": SCHEMA,
        "surfaces": [item.to_dict() for item in surfaces],
        "source_evidence": sorted(evidence, key=lambda item: item["surface_id"]),
        "structural_overlaps": overlaps,
        "family_recommendations": recommendations,
        "removal_authority": False,
        "executes": False,
        "mutates": False,
    }
    return {"review_id": _identity("ear_", payload), **payload}
