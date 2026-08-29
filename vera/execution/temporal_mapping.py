"""Static LIB-17 Temporal comparison; never imports or starts Temporal."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from .dbos_mapping import DBOSDurabilityMapping, compile_dbos_mapping
from .durability_fixture import DurabilityFixture, build_durability_fixture


TEMPORAL_MAPPING_SCHEMA = "vera.temporal-durability-mapping/v1"
TEMPORAL_PACKAGE = "temporalio==1.32.0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+|=@-]{0,255}\Z")


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _text(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 1200:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class TemporalStepMapping:
    step_id: str
    construct: str
    options: tuple[tuple[str, Any], ...] = ()
    input_bindings: tuple[str, ...] = ()
    output_binding: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "step ID"))
        object.__setattr__(self, "construct", _identifier(
            self.construct, "Temporal construct"))
        options = tuple(sorted(self.options))
        if len({key for key, _ in options}) != len(options):
            raise ValueError("Temporal step option keys must be unique")
        for key, value in options:
            _identifier(key, "Temporal option")
            if not isinstance(value, (str, int, float, bool)) or value is None:
                raise TypeError("Temporal options must be JSON scalars")
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "input_bindings", tuple(sorted({
            _identifier(item, "input binding") for item in self.input_bindings})))
        if self.output_binding:
            object.__setattr__(self, "output_binding", _identifier(
                self.output_binding, "output binding"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "construct": self.construct,
            "options": dict(self.options),
            "input_bindings": list(self.input_bindings),
            "output_binding": self.output_binding,
        }


@dataclass(frozen=True)
class TemporalMappingGap:
    code: str
    path: str
    detail: str
    blocking: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "gap code"))
        object.__setattr__(self, "path", _identifier(self.path, "gap path"))
        object.__setattr__(self, "detail", _text(self.detail, "gap detail"))
        if not isinstance(self.blocking, bool):
            raise TypeError("gap blocking must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TemporalDecisionRequirement:
    requirement: str
    temporal_candidate: str
    dbos_evidence: str
    disposition: str = "requires_live_evidence"

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement", _identifier(
            self.requirement, "decision requirement"))
        object.__setattr__(self, "temporal_candidate", _text(
            self.temporal_candidate, "Temporal candidate"))
        object.__setattr__(self, "dbos_evidence", _text(
            self.dbos_evidence, "DBOS evidence"))
        object.__setattr__(self, "disposition", _identifier(
            self.disposition, "decision disposition"))
        if self.disposition not in {
                "documented_candidate", "dbos_gap_unproven",
                "requires_live_evidence"}:
            raise ValueError("unsupported decision disposition")

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DBOSGapDisposition:
    gap_code: str
    temporal_relation: str
    disposition: str = "requires_live_evidence"

    def __post_init__(self) -> None:
        object.__setattr__(self, "gap_code", _identifier(
            self.gap_code, "DBOS gap code"))
        object.__setattr__(self, "temporal_relation", _text(
            self.temporal_relation, "Temporal relation"))
        object.__setattr__(self, "disposition", _identifier(
            self.disposition, "DBOS gap disposition"))
        if self.disposition not in {"same_gap", "different_projection",
                                    "requires_live_evidence"}:
            raise ValueError("unsupported DBOS gap disposition")

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TemporalDurabilityMapping:
    fixture_id: str
    workflow_content_hash: str
    definition_revision: str
    implementation_revision: str
    dbos_mapping_id: str
    package: str
    workflow_construct: str
    workflow_id_source: str
    versioning_sources: tuple[str, ...]
    steps: tuple[TemporalStepMapping, ...]
    status_mapping: tuple[tuple[str, str], ...]
    event_sources: tuple[tuple[str, str], ...]
    gaps: tuple[TemporalMappingGap, ...]
    decision_requirements: tuple[TemporalDecisionRequirement, ...]
    dbos_gap_dispositions: tuple[DBOSGapDisposition, ...]
    schema: str = TEMPORAL_MAPPING_SCHEMA
    mapping_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
                "fixture_id", "workflow_content_hash", "definition_revision",
                "implementation_revision", "dbos_mapping_id", "package",
                "workflow_construct", "workflow_id_source"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        versions = tuple(sorted({_identifier(item, "versioning source")
                                 for item in self.versioning_sources}))
        if not versions:
            raise ValueError("versioning_sources must not be empty")
        object.__setattr__(self, "versioning_sources", versions)
        steps = tuple(sorted(self.steps, key=lambda item: item.step_id))
        if not steps or not all(isinstance(item, TemporalStepMapping) for item in steps):
            raise ValueError("steps must contain TemporalStepMapping values")
        if len({item.step_id for item in steps}) != len(steps):
            raise ValueError("Temporal step mappings must be unique")
        object.__setattr__(self, "steps", steps)
        for field_name in ("status_mapping", "event_sources"):
            pairs = tuple(sorted(getattr(self, field_name)))
            if not pairs or len({key for key, _ in pairs}) != len(pairs):
                raise ValueError(f"{field_name} keys must be non-empty and unique")
            for key, value in pairs:
                _identifier(key, field_name + " key")
                _identifier(value, field_name + " value")
            object.__setattr__(self, field_name, pairs)
        gaps = tuple(sorted(self.gaps, key=lambda item: (item.path, item.code)))
        if not gaps or not all(isinstance(item, TemporalMappingGap) for item in gaps):
            raise ValueError("mapping must disclose TemporalMappingGap values")
        if len({(item.path, item.code) for item in gaps}) != len(gaps):
            raise ValueError("mapping gaps must be unique")
        object.__setattr__(self, "gaps", gaps)
        requirements = tuple(sorted(
            self.decision_requirements, key=lambda item: item.requirement))
        if not requirements or not all(
                isinstance(item, TemporalDecisionRequirement) for item in requirements):
            raise ValueError("decision requirements must be explicit")
        if len({item.requirement for item in requirements}) != len(requirements):
            raise ValueError("decision requirements must be unique")
        object.__setattr__(self, "decision_requirements", requirements)
        dispositions = tuple(sorted(
            self.dbos_gap_dispositions, key=lambda item: item.gap_code))
        if not dispositions or not all(
                isinstance(item, DBOSGapDisposition) for item in dispositions):
            raise ValueError("DBOS gap dispositions must be explicit")
        if len({item.gap_code for item in dispositions}) != len(dispositions):
            raise ValueError("DBOS gap dispositions must be unique")
        object.__setattr__(self, "dbos_gap_dispositions", dispositions)
        object.__setattr__(self, "mapping_id", "temporalmap_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    @property
    def ready_for_execution(self) -> bool:
        return not any(item.blocking for item in self.gaps)

    @property
    def decision_ready(self) -> bool:
        return self.ready_for_execution and all(
            item.disposition != "requires_live_evidence"
            for item in self.decision_requirements)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "fixture_id": self.fixture_id,
            "workflow_content_hash": self.workflow_content_hash,
            "definition_revision": self.definition_revision,
            "implementation_revision": self.implementation_revision,
            "dbos_mapping_id": self.dbos_mapping_id,
            "package": self.package,
            "workflow_construct": self.workflow_construct,
            "workflow_id_source": self.workflow_id_source,
            "versioning_sources": list(self.versioning_sources),
            "steps": [item.to_dict() for item in self.steps],
            "status_mapping": dict(self.status_mapping),
            "event_sources": dict(self.event_sources),
            "gaps": [item.to_dict() for item in self.gaps],
            "decision_requirements": [item.to_dict()
                                      for item in self.decision_requirements],
            "dbos_gap_dispositions": [item.to_dict()
                                      for item in self.dbos_gap_dispositions],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            **self.identity_dict(),
            "recommendation": "defer",
            "decision_ready": self.decision_ready,
            "live_pilot_approved": False,
            "ready_for_execution": self.ready_for_execution,
            "executes": False,
            "imports_runtime": False,
        }


def compile_temporal_mapping(
        fixture: DurabilityFixture | None = None,
        dbos_mapping: DBOSDurabilityMapping | None = None,
) -> TemporalDurabilityMapping:
    """Compile a static comparison manifest without importing Temporal."""
    fixture = fixture or build_durability_fixture()
    dbos_mapping = dbos_mapping or compile_dbos_mapping(fixture)
    if not isinstance(fixture, DurabilityFixture):
        raise TypeError("fixture must be DurabilityFixture")
    if not isinstance(dbos_mapping, DBOSDurabilityMapping):
        raise TypeError("dbos_mapping must be DBOSDurabilityMapping")
    workflow = fixture.workflow
    if (dbos_mapping.fixture_id != fixture.fixture_id or
            dbos_mapping.workflow_content_hash != workflow["content_hash"]):
        raise ValueError("DBOS mapping must bind the same LIB-15 fixture")
    steps_by_id = {step["id"]: step for step in workflow["steps"]}
    expected = {"prepare", "durable_wait", "retryable_step", "idempotent_effect", "finalize"}
    if set(steps_by_id) != expected:
        raise ValueError("Temporal mapping supports only the canonical LIB-15 step set")
    retry = steps_by_id["retryable_step"]["retry"]
    timeout = steps_by_id["retryable_step"]["timeout"]
    steps = (
        TemporalStepMapping("prepare", "workflow.execute_activity",
                            output_binding="prepared"),
        TemporalStepMapping("durable_wait", "workflow.sleep",
                            input_bindings=("wake_at",)),
        TemporalStepMapping(
            "retryable_step", "workflow.execute_activity",
            options=(("initial_interval_seconds", retry["backoff_seconds"]),
                     ("maximum_attempts", retry["max_attempts"]),
                     ("start_to_close_timeout_seconds", timeout["seconds"])),
            output_binding="retry_result"),
        TemporalStepMapping(
            "idempotent_effect", "workflow.execute_activity",
            options=(("cancellation_type", "WAIT_CANCELLATION_COMPLETED"),),
            input_bindings=("request_id",), output_binding="receipt"),
        TemporalStepMapping("finalize", "workflow.execute_activity",
                            input_bindings=("receipt",)),
    )
    statuses = (
        ("CANCELED", "cancelled"), ("COMPLETED", "completed"),
        ("CONTINUED_AS_NEW", "running"), ("FAILED", "failed"),
        ("RUNNING", "queued|running|waiting"), ("TERMINATED", "cancelled"),
        ("TIMED_OUT", "timed_out"),
    )
    events = (
        ("run.started", "WorkflowExecutionStarted"),
        ("run.waiting", "TimerStarted|adapter_checkpoint"),
        ("run.completed", "WorkflowExecutionCompleted"),
        ("run.failed", "WorkflowExecutionFailed"),
        ("run.cancelled", "WorkflowExecutionCanceled|WorkflowExecutionTerminated"),
        ("run.timed_out", "WorkflowExecutionTimedOut"),
        ("step.started", "ActivityTaskStarted"),
        ("step.completed", "ActivityTaskCompleted"),
        ("step.retrying", "ActivityTaskFailed|RetryPolicy"),
        ("run.recovery.started", "WorkflowTaskStarted|history_replay"),
        ("run.recovery.resumed", "WorkflowTaskCompleted|history_replay"),
        ("run.version_mismatch", "NondeterminismError|versioning_guard"),
        ("effect.receipt.recorded", "ActivityTaskCompleted|verified_artifact"),
    )
    gaps = (
        TemporalMappingGap(
            "absolute_wake_translation", "steps.durable_wait",
            "LIB-15 wake_at needs a deterministic conversion using workflow.now before "
            "workflow.sleep; timezone and already-due behavior require live proof."),
        TemporalMappingGap(
            "activity_receipt_atomicity_unproven", "steps.idempotent_effect",
            "Activity completion does not prove the external service deduplicated request_id "
            "or retained its receipt across a worker crash."),
        TemporalMappingGap(
            "event_projection_unproven", "events",
            "History events are candidates, not proof that Vera Run events remain complete, "
            "ordered, redacted, and stable across replay."),
        TemporalMappingGap(
            "version_strategy_unselected", "versioning",
            "workflow.patched/deprecate_patch and Worker Versioning are documented surfaces, "
            "but Vera has not selected or tested an in-flight migration policy."),
        TemporalMappingGap(
            "payload_boundary_unmapped", "payloads",
            "Payload codecs and ArtifactRef thresholds need a measured size, encryption, and "
            "redaction policy before Workflow history can carry Vera state."),
        TemporalMappingGap(
            "service_operability_unproven", "service",
            "No Temporal service, namespace, task queue, worker, retention, visibility, backup, "
            "or recovery topology has been operated for Vera."),
        TemporalMappingGap(
            "cancellation_boundary_unproven", "controls.cancel",
            "Activity cancellation type and heartbeats require a live crash/cancel test before "
            "Vera can promise acknowledgement and effect boundaries."),
    )
    requirements = (
        TemporalDecisionRequirement(
            "cross_service_workers", "Service task queues and Workers are a candidate.",
            "DBOS cross-service insufficiency has not been measured."),
        TemporalDecisionRequirement(
            "long_lived_history", "Event history and Continue-As-New are candidates.",
            "DBOS longevity or history limits have not been measured."),
        TemporalDecisionRequirement(
            "versioned_worker_routing", "Worker Versioning is a candidate.",
            "DBOS application-version and patch behavior has not failed a Vera fixture."),
        TemporalDecisionRequirement(
            "signals_updates", "Signals and Updates are message candidates.",
            "Vera has not shown a DBOS messaging requirement that cannot be adapted."),
        TemporalDecisionRequirement(
            "child_workflows", "Child Workflows are a composition candidate.",
            "A DBOS child-orchestration gap has not been measured."),
        TemporalDecisionRequirement(
            "schedules", "Temporal Schedules are a trigger candidate.",
            "A DBOS scheduling gap has not been measured."),
        TemporalDecisionRequirement(
            "retention_visibility", "History, search attributes, and visibility are candidates.",
            "Retention and query SLOs have not been tested for either runtime."),
        TemporalDecisionRequirement(
            "in_flight_migration", "Patching and Worker Versioning are candidates.",
            "DBOS and Temporal have not passed Vera's compatible-resume scenarios."),
    )
    relations = {
        "absolute_wake_translation": (
            "Temporal also needs deterministic wake_at conversion using workflow.now."),
        "timeout_cancel_ambiguity": (
            "Temporal projects TIMED_OUT separately, but Vera still must verify cancellation provenance."),
        "compatible_version_plan_missing": (
            "Temporal exposes patching and Worker Versioning candidates; no Vera migration plan is proven."),
        "effect_receipt_atomicity_unproven": (
            "Temporal Activity completion cannot prove external request deduplication or receipt retention."),
        "run_event_projection_unproven": (
            "Temporal history is richer candidate evidence, but Vera Run projection remains unproven."),
        "large_result_boundary_unmapped": (
            "Temporal payload/history limits still require Vera ArtifactRef thresholds and redaction policy."),
        "cancellation_boundary_unproven": (
            "Temporal cancellation types and Activity heartbeats still require live boundary evidence."),
    }
    dbos_codes = {gap.code for gap in dbos_mapping.gaps}
    if dbos_codes != set(relations):
        raise ValueError("Temporal comparison does not cover every DBOS mapping gap")
    dispositions = tuple(DBOSGapDisposition(code, relations[code])
                         for code in sorted(relations))
    return TemporalDurabilityMapping(
        fixture_id=fixture.fixture_id,
        workflow_content_hash=workflow["content_hash"],
        definition_revision=fixture.definition_revision,
        implementation_revision=fixture.implementation_revision,
        dbos_mapping_id=dbos_mapping.mapping_id,
        package=TEMPORAL_PACKAGE,
        workflow_construct="workflow.defn|workflow.run",
        workflow_id_source="Run.id|Client.start_workflow.id",
        versioning_sources=("workflow.patched", "workflow.deprecate_patch",
                            "WorkerDeploymentVersion|versioning_behavior"),
        steps=steps, status_mapping=statuses, event_sources=events,
        gaps=gaps, decision_requirements=requirements,
        dbos_gap_dispositions=dispositions)
