"""Vendor-neutral, non-executing durability semantics for runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping

from .workflow_ir import adapter_profiles, normalize_workflow


DURABILITY_SCHEMA = "vera.runtime-durability-fixture/v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
REQUIRED_SEMANTICS = (
    "cancellation",
    "crash_resume",
    "deterministic_steps",
    "durable_checkpoints",
    "durable_wait",
    "effect_receipts",
    "idempotency_key",
    "retry",
    "run_events",
    "timeout",
    "version_guard",
)
REQUIRED_EVENT_TYPES = (
    "effect.receipt.recorded",
    "run.cancelled",
    "run.completed",
    "run.failed",
    "run.recovery.resumed",
    "run.recovery.started",
    "run.started",
    "run.timed_out",
    "run.version_mismatch",
    "run.waiting",
    "step.completed",
    "step.retrying",
    "step.started",
)
_SCENARIO_KINDS = {"clean", "retry", "cancel", "timeout", "version_change", "crash"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class DurabilityScenario:
    scenario_id: str
    kind: str
    terminal_status: str
    expected_events: tuple[str, ...]
    crash_boundary: str = ""
    resume_after: str = ""
    attempts: int = 1
    effect_receipts: int = 0
    version_outcome: str = "unchanged"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _identifier(
            self.scenario_id, "scenario ID"))
        if self.kind not in _SCENARIO_KINDS:
            raise ValueError("unsupported durability scenario kind")
        if self.terminal_status not in _TERMINAL_STATUSES:
            raise ValueError("unsupported terminal status")
        events = tuple(_identifier(item, "event type") for item in self.expected_events)
        if not events:
            raise ValueError("scenario must declare expected events")
        object.__setattr__(self, "expected_events", events)
        for name in ("crash_boundary", "resume_after"):
            if getattr(self, name):
                object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if isinstance(self.attempts, bool) or self.attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if isinstance(self.effect_receipts, bool) or self.effect_receipts < 0:
            raise ValueError("effect_receipts must be non-negative")
        if self.version_outcome not in {"unchanged", "compatible_resume", "reject_mismatch"}:
            raise ValueError("unsupported version outcome")
        if self.kind == "crash" and (not self.crash_boundary or not self.resume_after):
            raise ValueError("crash scenarios require a boundary and resume point")
        if self.kind != "crash" and (self.crash_boundary or self.resume_after):
            raise ValueError("only crash scenarios declare crash boundaries")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "terminal_status": self.terminal_status,
            "expected_events": list(self.expected_events),
            "crash_boundary": self.crash_boundary,
            "resume_after": self.resume_after,
            "attempts": self.attempts,
            "effect_receipts": self.effect_receipts,
            "version_outcome": self.version_outcome,
        }


@dataclass(frozen=True)
class DurabilityFixture:
    definition_revision: str
    implementation_revision: str
    workflow_json: str
    crash_boundaries: tuple[str, ...]
    scenarios: tuple[DurabilityScenario, ...]
    requirements: tuple[str, ...] = REQUIRED_SEMANTICS
    schema: str = DURABILITY_SCHEMA
    fixture_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("definition_revision", "implementation_revision"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        try:
            workflow = normalize_workflow(json.loads(self.workflow_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("workflow_json must contain valid Workflow IR") from exc
        object.__setattr__(self, "workflow_json", _canonical(workflow))
        boundaries = tuple(_identifier(item, "crash boundary")
                           for item in self.crash_boundaries)
        if len(set(boundaries)) != len(boundaries) or not boundaries:
            raise ValueError("crash boundaries must be non-empty and unique")
        object.__setattr__(self, "crash_boundaries", boundaries)
        scenarios = tuple(self.scenarios)
        if not scenarios or not all(isinstance(item, DurabilityScenario) for item in scenarios):
            raise ValueError("scenarios must contain DurabilityScenario values")
        if len({item.scenario_id for item in scenarios}) != len(scenarios):
            raise ValueError("scenario IDs must be unique")
        covered = {item.crash_boundary for item in scenarios if item.kind == "crash"}
        if covered != set(boundaries):
            raise ValueError("crash scenarios must cover every declared boundary exactly once")
        if sum(item.kind == "crash" for item in scenarios) != len(boundaries):
            raise ValueError("crash boundaries cannot have duplicate scenarios")
        required_kinds = {"clean", "retry", "cancel", "timeout", "version_change", "crash"}
        if {item.kind for item in scenarios} != required_kinds:
            raise ValueError("fixture must cover every durability scenario kind")
        if any(set(item.expected_events) - set(REQUIRED_EVENT_TYPES) for item in scenarios):
            raise ValueError("scenarios may only use portable required Run event types")
        if set().union(*(set(item.expected_events) for item in scenarios)) != set(
                REQUIRED_EVENT_TYPES):
            raise ValueError("scenarios must collectively cover every required Run event type")
        terminal_events = {
            "completed": "run.completed", "failed": "run.failed",
            "cancelled": "run.cancelled", "timed_out": "run.timed_out"}
        if any(item.expected_events[-1] != terminal_events[item.terminal_status]
               for item in scenarios):
            raise ValueError("each scenario must end with its declared terminal Run event")
        if any(item.effect_receipts != (1 if item.terminal_status == "completed" else 0)
               for item in scenarios):
            raise ValueError("only completed scenarios retain exactly one effect receipt")
        version_outcomes = {item.version_outcome for item in scenarios
                            if item.kind == "version_change"}
        if version_outcomes != {"compatible_resume", "reject_mismatch"}:
            raise ValueError("version scenarios must cover compatible resume and mismatch refusal")
        if any(item.version_outcome != "unchanged" for item in scenarios
               if item.kind != "version_change"):
            raise ValueError("only version-change scenarios declare version outcomes")
        object.__setattr__(self, "scenarios", tuple(sorted(
            scenarios, key=lambda item: item.scenario_id)))
        requirements = tuple(sorted({_identifier(item, "requirement")
                                     for item in self.requirements}))
        if requirements != REQUIRED_SEMANTICS:
            raise ValueError("fixture requirements must match the durability contract")
        object.__setattr__(self, "requirements", requirements)
        object.__setattr__(self, "fixture_id", "durability_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    @property
    def workflow(self) -> dict[str, Any]:
        return json.loads(self.workflow_json)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "definition_revision": self.definition_revision,
            "implementation_revision": self.implementation_revision,
            "workflow": self.workflow,
            "crash_boundaries": list(self.crash_boundaries),
            "scenarios": [item.to_dict() for item in self.scenarios],
            "requirements": list(self.requirements),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"fixture_id": self.fixture_id, **self.identity_dict(), "executes": False}


@dataclass(frozen=True)
class RuntimeDurabilityProfile:
    adapter_id: str
    executable: bool
    supported_semantics: tuple[str, ...]
    event_types: tuple[str, ...]
    external_effect_delivery: str
    version_change_policy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _identifier(self.adapter_id, "adapter ID"))
        if not isinstance(self.executable, bool):
            raise TypeError("executable must be boolean")
        semantics = tuple(sorted({_identifier(item, "supported semantic")
                                  for item in self.supported_semantics}))
        unknown = set(semantics) - set(REQUIRED_SEMANTICS)
        if unknown:
            raise ValueError("profile contains unknown durability semantics")
        object.__setattr__(self, "supported_semantics", semantics)
        object.__setattr__(self, "event_types", tuple(sorted({
            _identifier(item, "event type") for item in self.event_types})))
        if self.external_effect_delivery not in {
                "unsupported", "at_least_once", "deduplicated_by_key", "exactly_once"}:
            raise ValueError("unsupported external effect delivery claim")
        if self.version_change_policy not in {"reject", "compatible_only", "ignore"}:
            raise ValueError("unsupported version change policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "executable": self.executable,
            "supported_semantics": list(self.supported_semantics),
            "event_types": list(self.event_types),
            "external_effect_delivery": self.external_effect_delivery,
            "version_change_policy": self.version_change_policy,
        }


def durability_profile_from_dict(value: Mapping[str, Any]) -> RuntimeDurabilityProfile:
    """Strictly reconstruct an adapter declaration supplied to an inspection API."""
    if not isinstance(value, Mapping):
        raise TypeError("profile must be an object")
    allowed = {
        "adapter_id", "executable", "supported_semantics", "event_types",
        "external_effect_delivery", "version_change_policy"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("unknown profile fields: " + ", ".join(unknown))
    try:
        return RuntimeDurabilityProfile(
            adapter_id=value["adapter_id"], executable=value["executable"],
            supported_semantics=tuple(value["supported_semantics"]),
            event_types=tuple(value["event_types"]),
            external_effect_delivery=value["external_effect_delivery"],
            version_change_policy=value["version_change_policy"])
    except KeyError as exc:
        raise ValueError(f"missing profile field: {exc.args[0]}") from exc


def build_durability_fixture() -> DurabilityFixture:
    """Return the canonical LIB-15 fixture; no workflow or effect is executed."""
    workflow = normalize_workflow({
        "ir_version": "1.0",
        "name": "portable-durability-fixture",
        "inputs": {
            "request_id": {"schema": {"type": "string"}, "required": True},
            "wake_at": {"schema": {"type": "string"}, "required": True},
        },
        "outputs": {"receipt": {"schema": {"type": "string"}, "required": True}},
        "steps": [
            {"id": "prepare", "type": "task", "task": "fixture.prepare",
             "output": "prepared"},
            {"id": "durable_wait", "type": "task", "task": "fixture.durable_wait",
             "bindings": {"wake_at": {"kind": "state", "value": "wake_at"}},
             "extensions": {"vera.semantic": "durable_wait"}},
            {"id": "retryable_step", "type": "task", "task": "fixture.retryable_step",
             "retry": {"max_attempts": 3, "backoff_seconds": 1, "owner": "runtime"},
             "timeout": {"seconds": 30, "owner": "runtime"}, "output": "retry_result"},
            {"id": "idempotent_effect", "type": "task", "task": "fixture.effect",
             "bindings": {"request_id": {"kind": "state", "value": "request_id"}},
             "idempotency": {"key": {"kind": "state", "value": "request_id"},
                             "owner": "runtime"},
             "effects": [{"kind": "external_service", "target": "fixture://sink",
                          "mode": "idempotent-with-receipt"}],
             "output": "receipt"},
            {"id": "finalize", "type": "task", "task": "fixture.finalize",
             "bindings": {"receipt": {"kind": "state", "value": "receipt"}}},
        ],
        "extensions": {
            "vera.durability": {
                "definition_revision": "fixture-definition-v1",
                "implementation_revision": "fixture-implementation-v1",
                "effect_guarantee": "deduplicated_by_key_not_exactly_once",
            }
        },
    })
    step_ids = tuple(step["id"] for step in workflow["steps"])
    boundaries = tuple(
        boundary for step_id in step_ids
        for boundary in (f"before:{step_id}", f"after:{step_id}"))
    scenarios = [
        DurabilityScenario(
            "clean-completion", "clean", "completed",
            ("run.started", "step.started", "step.completed",
             "effect.receipt.recorded", "run.completed"), effect_receipts=1),
        DurabilityScenario(
            "retry-then-complete", "retry", "completed",
            ("run.started", "step.started", "step.retrying", "step.started",
             "step.completed", "effect.receipt.recorded", "run.completed"),
            attempts=2, effect_receipts=1),
        DurabilityScenario(
            "cancel-during-wait", "cancel", "cancelled",
            ("run.started", "step.started", "run.waiting", "run.cancelled")),
        DurabilityScenario(
            "timeout-retryable-step", "timeout", "timed_out",
            ("run.started", "step.started", "run.timed_out")),
        DurabilityScenario(
            "compatible-version-resume", "version_change", "completed",
            ("run.started", "run.recovery.started", "run.recovery.resumed",
             "effect.receipt.recorded", "run.completed"),
            effect_receipts=1, version_outcome="compatible_resume"),
        DurabilityScenario(
            "reject-version-mismatch", "version_change", "failed",
            ("run.started", "run.recovery.started", "run.version_mismatch", "run.failed"),
            version_outcome="reject_mismatch"),
    ]
    scenarios.extend(
        DurabilityScenario(
            "crash-" + boundary.replace(":", "-"), "crash", "completed",
            ("run.started", "run.recovery.started", "run.recovery.resumed",
             "effect.receipt.recorded", "run.completed"),
            crash_boundary=boundary, resume_after=boundary, effect_receipts=1)
        for boundary in boundaries
    )
    return DurabilityFixture(
        definition_revision="fixture-definition-v1",
        implementation_revision="fixture-implementation-v1",
        workflow_json=_canonical(workflow), crash_boundaries=boundaries,
        scenarios=tuple(scenarios))


def analyze_durability_profile(
        profile: RuntimeDurabilityProfile,
        fixture: DurabilityFixture | None = None) -> dict[str, Any]:
    """Compare declared adapter semantics with LIB-15 without invoking the adapter."""
    if not isinstance(profile, RuntimeDurabilityProfile):
        raise TypeError("profile must be RuntimeDurabilityProfile")
    fixture = fixture or build_durability_fixture()
    if not isinstance(fixture, DurabilityFixture):
        raise TypeError("fixture must be DurabilityFixture")
    gaps: list[dict[str, str]] = []
    if not profile.executable:
        gaps.append({"code": "adapter_not_executable",
                     "detail": "profile is descriptive and cannot run the fixture"})
    for semantic in sorted(set(fixture.requirements) - set(profile.supported_semantics)):
        gaps.append({"code": "missing_semantic", "semantic": semantic,
                     "detail": f"adapter does not declare {semantic}"})
    for event_type in sorted(set(REQUIRED_EVENT_TYPES) - set(profile.event_types)):
        gaps.append({"code": "missing_run_event", "event_type": event_type,
                     "detail": f"adapter does not project {event_type}"})
    if profile.external_effect_delivery == "exactly_once":
        gaps.append({"code": "unsupported_guarantee",
                     "detail": "external effects cannot claim exactly-once delivery"})
    elif profile.external_effect_delivery != "deduplicated_by_key":
        gaps.append({"code": "effect_delivery_gap",
                     "detail": "fixture requires an idempotency key and durable receipt"})
    if profile.version_change_policy != "compatible_only":
        gaps.append({"code": "version_policy_gap",
                     "detail": "runtime must resume only a declared compatible implementation"})
    return {
        "ok": not gaps,
        "adapter_id": profile.adapter_id,
        "fixture_id": fixture.fixture_id,
        "gaps": gaps,
        "scenario_count": len(fixture.scenarios),
        "crash_boundary_count": len(fixture.crash_boundaries),
        "executes": False,
    }


def analyze_workflow_adapter_durability(adapter_id: str) -> dict[str, Any]:
    """Project a built-in Workflow IR profile into LIB-15 without loading a runtime."""
    adapter_id = _identifier(adapter_id, "adapter ID")
    raw = adapter_profiles()["profiles"].get(adapter_id)
    if raw is None:
        return {
            "ok": False, "adapter_id": adapter_id,
            "gaps": [{"code": "unknown_adapter",
                      "detail": "Workflow IR adapter profile is not registered"}],
            "executes": False,
        }
    semantic_mapping = {
        "tasks": "deterministic_steps",
        "retry": "retry",
        "timeout": "timeout",
        "idempotency": "idempotency_key",
    }
    supported = tuple(semantic_mapping[item] for item in raw.get("supports", ())
                      if item in semantic_mapping)
    report = analyze_durability_profile(RuntimeDurabilityProfile(
        adapter_id=adapter_id,
        executable=bool(raw.get("available") and raw.get("executable")),
        supported_semantics=supported,
        event_types=(),
        external_effect_delivery="unsupported",
        version_change_policy="ignore",
    ))
    return {**report, "available": bool(raw.get("available")),
            "profile_detail": str(raw.get("detail") or "")}
