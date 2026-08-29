"""Static LIB-15 to DBOS mapping; this module never imports or starts DBOS."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from .durability_fixture import DurabilityFixture, build_durability_fixture


DBOS_MAPPING_SCHEMA = "vera.dbos-durability-mapping/v1"
DBOS_PACKAGE = "dbos==2.30.0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+|=-]{0,255}\Z")


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class DBOSStepMapping:
    step_id: str
    construct: str
    options: tuple[tuple[str, Any], ...] = ()
    input_bindings: tuple[str, ...] = ()
    output_binding: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "step ID"))
        object.__setattr__(self, "construct", _identifier(self.construct, "DBOS construct"))
        options = tuple(sorted(self.options))
        if len({key for key, _ in options}) != len(options):
            raise ValueError("DBOS step option keys must be unique")
        for key, value in options:
            _identifier(key, "DBOS option")
            if not isinstance(value, (str, int, float, bool)) or value is None:
                raise TypeError("DBOS options must be JSON scalars")
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
class DBOSMappingGap:
    code: str
    path: str
    detail: str
    blocking: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "gap code"))
        object.__setattr__(self, "path", _identifier(self.path, "gap path"))
        detail = str(self.detail or "").strip()
        if not detail or len(detail) > 1000:
            raise ValueError("gap detail must be a bounded non-empty string")
        object.__setattr__(self, "detail", detail)
        if not isinstance(self.blocking, bool):
            raise TypeError("gap blocking must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DBOSDurabilityMapping:
    fixture_id: str
    workflow_content_hash: str
    definition_revision: str
    implementation_revision: str
    package: str
    workflow_construct: str
    workflow_id_source: str
    application_version_source: str
    steps: tuple[DBOSStepMapping, ...]
    status_mapping: tuple[tuple[str, str], ...]
    event_sources: tuple[tuple[str, str], ...]
    gaps: tuple[DBOSMappingGap, ...]
    schema: str = DBOS_MAPPING_SCHEMA
    mapping_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
                "fixture_id", "workflow_content_hash", "definition_revision",
                "implementation_revision", "package", "workflow_construct",
                "workflow_id_source", "application_version_source"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        steps = tuple(sorted(self.steps, key=lambda item: item.step_id))
        if not steps or not all(isinstance(item, DBOSStepMapping) for item in steps):
            raise ValueError("steps must contain DBOSStepMapping values")
        if len({item.step_id for item in steps}) != len(steps):
            raise ValueError("DBOS step mappings must be unique")
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
        if not gaps or not all(isinstance(item, DBOSMappingGap) for item in gaps):
            raise ValueError("mapping must disclose DBOSMappingGap values")
        if len({(item.path, item.code) for item in gaps}) != len(gaps):
            raise ValueError("mapping gaps must be unique")
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "mapping_id", "dbosmap_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    @property
    def ready_for_execution(self) -> bool:
        return not any(item.blocking for item in self.gaps)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "fixture_id": self.fixture_id,
            "workflow_content_hash": self.workflow_content_hash,
            "definition_revision": self.definition_revision,
            "implementation_revision": self.implementation_revision,
            "package": self.package,
            "workflow_construct": self.workflow_construct,
            "workflow_id_source": self.workflow_id_source,
            "application_version_source": self.application_version_source,
            "steps": [item.to_dict() for item in self.steps],
            "status_mapping": dict(self.status_mapping),
            "event_sources": dict(self.event_sources),
            "gaps": [item.to_dict() for item in self.gaps],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            **self.identity_dict(),
            "ready_for_execution": self.ready_for_execution,
            "executes": False,
            "imports_runtime": False,
        }


def compile_dbos_mapping(fixture: DurabilityFixture | None = None) -> DBOSDurabilityMapping:
    """Compile a review manifest only; no Python source or DBOS object is produced."""
    fixture = fixture or build_durability_fixture()
    if not isinstance(fixture, DurabilityFixture):
        raise TypeError("fixture must be DurabilityFixture")
    workflow = fixture.workflow
    steps_by_id = {step["id"]: step for step in workflow["steps"]}
    expected = {"prepare", "durable_wait", "retryable_step", "idempotent_effect", "finalize"}
    if set(steps_by_id) != expected:
        raise ValueError("DBOS mapping supports only the canonical LIB-15 step set")

    retry = steps_by_id["retryable_step"]["retry"]
    timeout = steps_by_id["retryable_step"]["timeout"]
    mappings = (
        DBOSStepMapping("prepare", "DBOS.step", output_binding="prepared"),
        DBOSStepMapping(
            "durable_wait", "DBOS.sleep", input_bindings=("wake_at",)),
        DBOSStepMapping(
            "retryable_step", "DBOS.step",
            options=(("retries_allowed", True),
                     ("interval_seconds", retry["backoff_seconds"]),
                     ("max_attempts", retry["max_attempts"]),
                     ("timeout_seconds", timeout["seconds"]),
                     ("preemptible", True)),
            output_binding="retry_result"),
        DBOSStepMapping(
            "idempotent_effect", "DBOS.step",
            options=(("preemptible", False),), input_bindings=("request_id",),
            output_binding="receipt"),
        DBOSStepMapping(
            "finalize", "DBOS.step", input_bindings=("receipt",)),
    )
    statuses = (
        ("CANCELLED", "cancelled|timed_out"),
        ("DELAYED", "waiting"),
        ("ENQUEUED", "queued"),
        ("ERROR", "failed"),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", "failed"),
        ("PENDING", "running"),
        ("SUCCESS", "completed"),
    )
    event_sources = (
        ("run.started", "WorkflowStatus.PENDING"),
        ("run.waiting", "WorkflowStatus.DELAYED|adapter_checkpoint"),
        ("run.completed", "WorkflowStatus.SUCCESS"),
        ("run.failed", "WorkflowStatus.ERROR|MAX_RECOVERY_ATTEMPTS_EXCEEDED"),
        ("run.cancelled", "WorkflowStatus.CANCELLED|control_receipt"),
        ("run.timed_out", "WorkflowStatus.CANCELLED|timeout_deadline"),
        ("step.started", "adapter_observer"),
        ("step.completed", "list_workflow_steps"),
        ("step.retrying", "adapter_observer"),
        ("run.recovery.started", "adapter_recovery_observer"),
        ("run.recovery.resumed", "adapter_recovery_observer"),
        ("run.version_mismatch", "application_version_guard"),
        ("effect.receipt.recorded", "verified_step_result_artifact"),
    )
    gaps = (
        DBOSMappingGap(
            "absolute_wake_translation", "steps.durable_wait",
            "LIB-15 supplies wake_at while DBOS.sleep accepts seconds; the adapter needs a "
            "persisted, deterministic conversion and clock policy."),
        DBOSMappingGap(
            "timeout_cancel_ambiguity", "status.CANCELLED",
            "DBOS represents timeout as cancellation; the adapter must retain the originating "
            "timeout deadline/control receipt to project the correct Run terminal state."),
        DBOSMappingGap(
            "compatible_version_plan_missing", "application_version",
            "Exact application-version recovery is mappable, but compatible resume needs a "
            "reviewed DBOS.patch/deprecate_patch plan bound to the fixture revisions."),
        DBOSMappingGap(
            "effect_receipt_atomicity_unproven", "steps.idempotent_effect",
            "A DBOS step checkpoint does not prove an independent service deduplicated the "
            "request; live evidence must verify the key and retained receipt across crashes."),
        DBOSMappingGap(
            "run_event_projection_unproven", "events",
            "Several required Run events need adapter observations beyond WorkflowStatus and "
            "list_workflow_steps; completeness and ordering require execution evidence."),
        DBOSMappingGap(
            "large_result_boundary_unmapped", "step_results",
            "The adapter must store large or sensitive results as ArtifactRefs rather than DBOS "
            "workflow state, with a measured size and redaction policy."),
        DBOSMappingGap(
            "cancellation_boundary_unproven", "controls.cancel",
            "Synchronous non-preemptible effect steps stop only at a later boundary; the live "
            "adapter must prove cancellation acknowledgement and child behavior."),
    )
    return DBOSDurabilityMapping(
        fixture_id=fixture.fixture_id,
        workflow_content_hash=workflow["content_hash"],
        definition_revision=fixture.definition_revision,
        implementation_revision=fixture.implementation_revision,
        package=DBOS_PACKAGE,
        workflow_construct="DBOS.workflow",
        workflow_id_source="Run.id|SetWorkflowID",
        application_version_source="fixture.implementation_revision",
        steps=mappings, status_mapping=statuses, event_sources=event_sources,
        gaps=gaps)
