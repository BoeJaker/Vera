"""Source-bound inventory for ADOPT-07A execution producers.

This module is deliberately pure: it reads source evidence but imports none of
the agent-loop, DAG, Dream, calendar, Flow Builder, Run, or Workflow modules it
describes.  The inventory distinguishes native execution owners from
definition, control, observation, and projection paths.  It is evidence for
later owning-path repairs, not a new dispatcher or an authority migration.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Sequence


SCHEMA = "vera.execution-producer-path-baseline/v1"
_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_FAMILIES = {"agent-loop", "dag", "dream", "schedule", "flow-builder"}
_ROLES = {"native-executor", "native-control", "native-scheduler",
          "definition", "projection", "observer"}
_STATES = {"live", "scheduled", "read_only", "definition_only"}
_COVERAGE = {"native", "projected", "partial", "absent", "not_applicable"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _source_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
        raise ValueError("source must be a repository-relative Python file")
    return path


@dataclass(frozen=True, slots=True)
class ExecutionProducerPath:
    path_id: str
    family: str
    role: str
    authority: str
    runtime_state: str
    source: str
    source_assertion: str
    entrypoint: str
    identities: tuple[str, ...]
    progress: str
    cancellation: str
    artifacts: str
    effects: str
    recovery: str
    target: str = ""

    def __post_init__(self) -> None:
        for name in ("path_id", "authority", "entrypoint"):
            if not _ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a bounded identifier")
        if self.target and not _ID.fullmatch(self.target):
            raise ValueError("target must be a bounded identifier")
        if self.family not in _FAMILIES or self.role not in _ROLES:
            raise ValueError("unsupported family or role")
        if self.runtime_state not in _STATES:
            raise ValueError("unsupported runtime state")
        if not self.identities or len(set(self.identities)) != len(self.identities):
            raise ValueError("identities must be non-empty and unique")
        if any(not _ID.fullmatch(value) for value in self.identities):
            raise ValueError("identity fields must be bounded identifiers")
        for dimension in ("progress", "cancellation", "artifacts", "effects", "recovery"):
            if getattr(self, dimension) not in _COVERAGE:
                raise ValueError(f"unsupported {dimension} coverage")
        _source_path(self.source)
        if not self.source_assertion or len(self.source_assertion) > 256:
            raise ValueError("source_assertion must be present and bounded")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id, "family": self.family, "role": self.role,
            "authority": self.authority, "runtime_state": self.runtime_state,
            "source": self.source, "entrypoint": self.entrypoint,
            "identities": list(self.identities), "progress": self.progress,
            "cancellation": self.cancellation, "artifacts": self.artifacts,
            "effects": self.effects, "recovery": self.recovery,
            "target": self.target,
        }


PATHS = (
    ExecutionProducerPath(
        "agent-loop.http-stream", "agent-loop", "native-executor", "agent-loop",
        "live", "vera/dag/dag_workshop_capabilities.py",
        "async def workshop_agent_loop_stream(request: Request):",
        "workshop.agent-loop.stream", ("session-id", "engine", "profile"),
        "native", "native", "partial", "partial", "native",
        "agent-loop.run-projection"),
    ExecutionProducerPath(
        "agent-loop.run-projection", "agent-loop", "projection", "run-protocol",
        "live", "vera/execution/agent_loop_run_projection.py",
        "class AgentLoopRunProjection:", "run.agent-loop.project",
        ("run-id", "session-id", "workflow-id", "task-id"),
        "projected", "projected", "projected", "partial", "projected"),
    ExecutionProducerPath(
        "agent-loop.program", "agent-loop", "native-executor", "loop-orchestrator",
        "scheduled", "vera/dag/loop_orchestrator.py",
        "async def _run_program_loop(prog: Dict[str, Any], lp: Dict[str, Any]) -> None:",
        "loops.program.execute", ("program-id", "loop-name", "session-id", "run-number"),
        "native", "native", "native", "partial", "native"),
    ExecutionProducerPath(
        "agent-loop.program-control", "agent-loop", "native-control", "loop-orchestrator",
        "live", "vera/dag/loop_orchestrator.py",
        '"loops.program.cancel", memory="off",', "loops.program.cancel",
        ("program-id", "session-id"), "native", "native", "partial", "partial", "native",
        "agent-loop.program"),
    ExecutionProducerPath(
        "dag.stored-run", "dag", "native-executor", "dag-store", "live",
        "vera/dag/dag_store.py", '"dag.store_run", memory="on",',
        "dag.store-run", ("dag-id", "trace-id", "session-id", "workflow-hash"),
        "partial", "absent", "absent", "partial", "native",
        "dag.memory-observer"),
    ExecutionProducerPath(
        "dag.monitored-run", "dag", "native-executor", "execution-monitor", "live",
        "vera/dag/dag_store.py", '"dag.run_monitored", memory="on",',
        "dag.run-monitored", ("trace-id", "session-id", "workflow-hash"),
        "native", "absent", "absent", "partial", "native",
        "dag.memory-observer"),
    ExecutionProducerPath(
        "dag.workflow-definition", "dag", "definition", "workflow-ir", "live",
        "vera/execution/dag_workflow_execution.py",
        "def prepare_plain_dag_execution(graph: list) -> dict[str, Any]:",
        "dag.workflow.prepare", ("workflow-hash", "ir-version", "control-mode"),
        "not_applicable", "not_applicable", "not_applicable", "not_applicable",
        "not_applicable", "dag.stored-run"),
    ExecutionProducerPath(
        "dag.memory-observer", "dag", "observer", "memory", "live",
        "vera/execution/workflow_memory_observer.py",
        "async def observe_completed_workflow(", "dag.memory.observe",
        ("session-id", "trigger"), "absent", "absent", "absent", "absent", "partial"),
    ExecutionProducerPath(
        "dream.cycle", "dream", "native-executor", "dream", "live",
        "vera/dream/dream_capabilities.py", "async def _run_cycle_native(",
        "dream.cycle.native", ("cycle-id", "trigger-name", "project-id", "journal-id"),
        "native", "native", "native", "native", "native",
        "dream.run-projection"),
    ExecutionProducerPath(
        "dream.run-projection", "dream", "projection", "run-protocol", "live",
        "vera/execution/dream_run_projection.py", "class DreamRunProjection:",
        "run.dream.project", ("run-id", "cycle-id", "workflow-id", "stage-id"),
        "projected", "projected", "absent", "partial", "projected"),
    ExecutionProducerPath(
        "dream.project-entry", "dream", "native-executor", "project", "live",
        "vera/dream/project_capabilities.py",
        "async def project_dream_run(slug: str, trigger_name: str = \"\",",
        "project.dream-run", ("project-slug", "cycle-id", "trigger-name"),
        "native", "partial", "native", "native", "native", "dream.cycle"),
    ExecutionProducerPath(
        "dream.scheduler", "dream", "native-scheduler", "dream", "scheduled",
        "vera/dream/dream_capabilities.py", "async def _scheduler_loop():",
        "dream.scheduler.loop", ("trigger-name", "schedule-id", "cycle-id"),
        "native", "native", "native", "native", "native", "dream.cycle"),
    ExecutionProducerPath(
        "schedule.calendar-action", "schedule", "native-executor", "calendar", "scheduled",
        "vera/calendar/longterm_scheduler.py",
        "async def _run_system_action_native(action: Dict[str, Any], cfg: Dict[str, Any]) -> None:",
        "calendar.system-action", ("action-id", "session-id", "schedule-id"),
        "native", "partial", "absent", "native", "native",
        "schedule.run-projection"),
    ExecutionProducerPath(
        "schedule.tick", "schedule", "native-scheduler", "calendar", "scheduled",
        "vera/calendar/longterm_scheduler.py", "async def _tick():",
        "calendar.scheduler.tick", ("action-id", "due-kind", "schedule-id"),
        "native", "partial", "absent", "native", "native", "schedule.calendar-action"),
    ExecutionProducerPath(
        "schedule.run-projection", "schedule", "projection", "run-protocol", "live",
        "vera/execution/schedule_run_projection.py", "class ScheduledRunProjection:",
        "run.schedule.project", ("run-id", "native-id", "schedule-id", "occurrence"),
        "projected", "projected", "absent", "partial", "projected"),
    ExecutionProducerPath(
        "flow-builder.definition", "flow-builder", "definition", "flow-builder",
        "definition_only", "vera/elements/flow_builder_capabilities.py",
        "description=\"Analyze a Flow Builder graph for loss-aware Workflow IR compatibility. Does not execute work.\"",
        "flow-builder.analyze", ("graph-digest", "workflow-hash"),
        "not_applicable", "not_applicable", "not_applicable", "not_applicable",
        "not_applicable", "dag.workflow-definition"),
)


GAPS = (
    "agent_loop_effect_and_artifact_coverage_is_event_derived_not_one_owning_receipt",
    "v8_program_execution_is_not_joined_to_one_parent_shared_run",
    "plain_and_monitored_dag_paths_have_no_shared_cancellation_contract",
    "dag_artifacts_and_effects_are_not_bound_to_shared_run_receipts",
    "dream_native_artifacts_and_effects_are_not_projected_on_the_shared_run",
    "calendar_action_has_no_artifact_contract_and_only_task_level_cancellation",
    "schedule_projection_observes_terminal_state_but_does_not_own_retry_or_recovery",
    "flow_builder_defines_workflows_but_has_no_execution_entrypoint",
    "producer_families_do_not_share_one_progress_cancel_artifact_effect_recovery_matrix",
)


def semantic_baseline(paths: Sequence[ExecutionProducerPath] = PATHS) -> dict[str, Any]:
    ordered = tuple(sorted(paths, key=lambda value: value.path_id))
    ids = [value.path_id for value in ordered]
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("paths must be non-empty and uniquely identified")
    payload = {
        "schema": SCHEMA,
        "paths": [value.semantic_dict() for value in ordered],
        "gaps": list(GAPS),
        "authority_moved": False,
        "runtime_changed": False,
        "coverage_complete": False,
    }
    return {"baseline_id": _identity("eppb_", payload), **payload}


def _source_evidence(repo_root: Path,
                     paths: Sequence[ExecutionProducerPath]) -> list[dict[str, str]]:
    root = repo_root.resolve()
    evidence = []
    for value in sorted(paths, key=lambda item: item.path_id):
        path = (root / value.source).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"source is unavailable: {value.source}")
        raw = path.read_bytes()
        if len(raw) > 4_000_000:
            raise ValueError(f"source is too large: {value.source}")
        if value.source_assertion not in raw.decode("utf-8"):
            raise ValueError(f"source assertion missing for {value.path_id}")
        evidence.append({
            "path_id": value.path_id,
            "source_path_digest": "sha256:" + hashlib.sha256(value.source.encode()).hexdigest(),
            "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digest": "sha256:" + hashlib.sha256(
                value.source_assertion.encode()).hexdigest(),
        })
    return evidence


def build_execution_producer_path_baseline(
        repo_root: Path,
        paths: Sequence[ExecutionProducerPath] = PATHS) -> dict[str, Any]:
    stable = semantic_baseline(paths)
    evidence = _source_evidence(Path(repo_root), paths)
    by_family: dict[str, list[str]] = {}
    by_role: dict[str, list[str]] = {}
    for value in sorted(paths, key=lambda item: item.path_id):
        by_family.setdefault(value.family, []).append(value.path_id)
        by_role.setdefault(value.role, []).append(value.path_id)
    return {
        **stable,
        "inventory_id": _identity(
            "eppi_", {"baseline_id": stable["baseline_id"],
                      "source_evidence": evidence}),
        "source_evidence": evidence,
        "paths_by_family": dict(sorted(by_family.items())),
        "paths_by_role": dict(sorted(by_role.items())),
        "counts": {
            "paths": len(paths), "families": len(by_family), "roles": len(by_role),
            "native_executors": sum(value.role == "native-executor" for value in paths),
            "projections": sum(value.role == "projection" for value in paths),
            "definition_only": sum(value.runtime_state == "definition_only" for value in paths),
            "gaps": len(GAPS),
        },
        "constraints": {
            "imports_inventoried_subsystems": False,
            "invokes_capabilities": False,
            "executes_workflows": False,
            "contacts_nodes": False,
            "contacts_models": False,
            "mutates": False,
        },
    }
