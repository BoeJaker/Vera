"""Versioned, runtime-neutral execution records for Vera and external engines."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Mapping
from uuid import uuid4


PROTOCOL_VERSION = "vera.run.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    APPROVAL_PENDING = "approval_pending"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


TERMINAL_STATUSES = {
    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT,
    RunStatus.SKIPPED,
}

ALLOWED_TRANSITIONS = {
    RunStatus.CREATED: {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLED,
                        RunStatus.SKIPPED},
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.TIMED_OUT},
    RunStatus.RUNNING: {RunStatus.WAITING, RunStatus.APPROVAL_PENDING,
                        RunStatus.RETRYING, *TERMINAL_STATUSES},
    RunStatus.WAITING: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.TIMED_OUT},
    RunStatus.APPROVAL_PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED,
                                 RunStatus.TIMED_OUT},
    RunStatus.RETRYING: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED,
                         RunStatus.TIMED_OUT},
}


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    kind: str
    uri: str
    checksum: str = ""
    media_type: str = ""
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip() or not self.uri.strip():
            raise ValueError("artifact id, kind, and uri are required")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")


@dataclass(frozen=True)
class RunError:
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("error code and message are required")


@dataclass(frozen=True)
class RunControl:
    id: str
    run_id: str
    action: str
    requested_at: str
    requested_by: str = ""
    status: str = "requested"
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.run_id.strip() or not self.action.strip():
            raise ValueError("control id, run id, and action are required")
        if self.status not in {"requested", "acknowledged", "rejected"}:
            raise ValueError("invalid run control status")


@dataclass(frozen=True)
class RunEvent:
    id: str
    run_id: str
    sequence: int
    type: str
    status: RunStatus
    occurred_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.run_id.strip() or not self.type.strip():
            raise ValueError("event id, run id, and type are required")
        if self.sequence < 1:
            raise ValueError("event sequence must be at least 1")
        object.__setattr__(self, "status", RunStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["protocol"] = PROTOCOL_VERSION
        return value


@dataclass
class Run:
    id: str
    kind: str
    status: RunStatus = RunStatus.CREATED
    parent_run_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    session_id: str = ""
    trace_id: str = ""
    attempt: int = 1
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    ended_at: str = ""
    progress: float | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)
    retry_owner: str = ""
    error: RunError | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    events: list[RunEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip():
            raise ValueError("run id and kind are required")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        self.status = RunStatus(self.status)
        _validate_progress(self.progress)
        for name, value in (("usage", self.usage), ("cost", self.cost),
                            ("policy", self.policy)):
            if not isinstance(value, Mapping):
                raise ValueError(f"run {name} must be a mapping")

    def transition(self, status: RunStatus | str, *, event_type: str = "",
                   payload: Mapping[str, Any] | None = None,
                   causation_id: str = "", occurred_at: str = "") -> RunEvent:
        target = RunStatus(status)
        if target not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"invalid run transition: {self.status.value} -> {target.value}")
        event_payload = dict(payload or {})
        _apply_observation(self, event_payload)
        when = occurred_at or utc_now()
        self.status = target
        if target == RunStatus.RUNNING and not self.started_at:
            self.started_at = when
        if target in TERMINAL_STATUSES:
            self.ended_at = when
        event = RunEvent(
            id=str(uuid4()), run_id=self.id, sequence=len(self.events) + 1,
            type=event_type or target.value, status=target, occurred_at=when,
            payload=event_payload, causation_id=causation_id,
        )
        self.events.append(event)
        return event

    def record_event(self, event_type: str, *, payload: Mapping[str, Any] | None = None,
                     causation_id: str = "", occurred_at: str = "") -> RunEvent:
        """Append observational progress without manufacturing a state transition."""
        event_payload = dict(payload or {})
        _apply_observation(self, event_payload)
        event = RunEvent(
            id=str(uuid4()), run_id=self.id, sequence=len(self.events) + 1,
            type=event_type, status=self.status, occurred_at=occurred_at or utc_now(),
            payload=event_payload, causation_id=causation_id,
        )
        self.events.append(event)
        return event

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["error"] = asdict(self.error) if self.error else None
        value["artifacts"] = [asdict(item) for item in self.artifacts]
        value["events"] = [item.to_dict() for item in self.events] if include_events else []
        value["protocol"] = PROTOCOL_VERSION
        return value


def _validate_progress(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("run progress must be a finite number from 0 to 1") from exc
    if not math.isfinite(number) or number < 0 or number > 1:
        raise ValueError("run progress must be a finite number from 0 to 1")
    return number


def _apply_observation(run: Run, payload: Mapping[str, Any]) -> None:
    """Apply portable observation fields without assigning execution authority."""
    progress = _validate_progress(payload.get("progress")) if "progress" in payload else None
    mappings = {}
    for name in ("usage", "cost", "policy"):
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, Mapping):
            raise ValueError(f"run {name} observation must be a mapping")
        mappings[name] = dict(value)
    attempt = None
    for name in ("attempt", "next_attempt"):
        if name in payload:
            try:
                candidate = int(payload[name])
            except (TypeError, ValueError) as exc:
                raise ValueError("run attempt observation must be at least 1") from exc
            if candidate < 1:
                raise ValueError("run attempt observation must be at least 1")
            attempt = max(attempt or 1, candidate)
    retry_owner = None
    if "retry_owner" in payload:
        retry_owner = str(payload["retry_owner"] or "").strip()

    if "progress" in payload:
        run.progress = progress
    for name, value in mappings.items():
        setattr(run, name, {**dict(getattr(run, name)), **value})
    if attempt is not None:
        run.attempt = max(run.attempt, attempt)
    if retry_owner is not None:
        run.retry_owner = retry_owner


def replay_run(run: Run, events: list[RunEvent]) -> Run:
    """Replay validated, gap-free events into a fresh run projection."""
    for expected, event in enumerate(events, start=len(run.events) + 1):
        if event.run_id != run.id:
            raise ValueError("event belongs to another run")
        if event.sequence != expected:
            raise ValueError("event sequence is not monotonic and gap-free")
        observational = event.status == run.status
        if not observational and event.status not in ALLOWED_TRANSITIONS.get(run.status, set()):
            raise ValueError(
                f"invalid run transition: {run.status.value} -> {event.status.value}")
        _apply_observation(run, event.payload)
        run.status = event.status
        if event.status == RunStatus.RUNNING and not run.started_at:
            run.started_at = event.occurred_at
        if event.status in TERMINAL_STATUSES:
            run.ended_at = event.occurred_at
        run.events.append(event)
    return run
