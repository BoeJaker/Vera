"""Versioned, content-safe trigger evidence shared by native schedulers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "vera.workflow-trigger/v1"
EVENT_TYPE = "workflow.trigger.emitted"
SOURCE_KINDS = {"calendar.action", "dream.trigger"}
SCHEDULE_KINDS = {"time", "condition", "idle_interval"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _zoned_time(name: str, value: Any) -> str:
    text = _required_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return text


def _timezone(value: Any) -> str:
    name = _required_text("timezone", value)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA timezone") from exc
    return name


def build_workflow_trigger(
    *, source_kind: str, source_id: str, source_revision: str,
    target_ref: str, schedule_kind: str, occurrence_key: str,
    observed_at: str, timezone_name: str = "UTC", scheduled_for: str = "",
    native_owner: str,
) -> dict[str, Any]:
    """Build deterministic trigger evidence; this function never executes work."""
    source_kind = _required_text("source_kind", source_kind)
    if source_kind not in SOURCE_KINDS:
        raise ValueError("source_kind is unsupported")
    schedule_kind = _required_text("schedule_kind", schedule_kind)
    if schedule_kind not in SCHEDULE_KINDS:
        raise ValueError("schedule_kind is unsupported")
    source_id = _required_text("source_id", source_id)
    source_revision = _required_text("source_revision", source_revision)
    target_ref = _required_text("target_ref", target_ref)
    occurrence_key = _required_text("occurrence_key", occurrence_key)
    native_owner = _required_text("native_owner", native_owner)
    observed_at = _zoned_time("observed_at", observed_at)
    timezone_name = _timezone(timezone_name)
    if scheduled_for:
        scheduled_for = _zoned_time("scheduled_for", scheduled_for)
    if schedule_kind == "time" and not scheduled_for:
        raise ValueError("scheduled_for is required for time schedules")
    if schedule_kind != "time" and scheduled_for:
        raise ValueError("scheduled_for is only valid for time schedules")

    identity = {
        "schema": SCHEMA,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_revision": source_revision,
        "occurrence_key": occurrence_key,
        "target_ref": target_ref,
        "schedule_kind": schedule_kind,
        "timezone": timezone_name,
        "scheduled_for": scheduled_for,
        "native_owner": native_owner,
    }
    trigger_id = _hash(identity)
    envelope = {
        "type": EVENT_TYPE,
        "schema": SCHEMA,
        "trigger_id": trigger_id,
        "idempotency_key": trigger_id,
        "source": {
            "kind": source_kind,
            "id": source_id,
            "revision": source_revision,
        },
        "target": {"ref": target_ref},
        "schedule": {
            "kind": schedule_kind,
            "timezone": timezone_name,
            "scheduled_for": scheduled_for,
        },
        "occurrence": {"key": occurrence_key, "observed_at": observed_at},
        "authority": {
            "scheduler": native_owner,
            "execution": "native",
            "projection": "workflow_trigger",
        },
        "policy": {
            "duplicates": "native_unverified",
            "misfire": "native_unverified",
            "catch_up": "native_unverified",
        },
        "executes": False,
    }
    validate_workflow_trigger(envelope)
    return envelope


def validate_workflow_trigger(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed envelope and return a detached JSON-safe copy."""
    if not isinstance(value, Mapping):
        raise TypeError("workflow trigger must be an object")
    allowed = {
        "type", "schema", "trigger_id", "idempotency_key", "source",
        "target", "schedule", "occurrence", "authority", "policy", "executes",
    }
    if set(value) != allowed:
        raise ValueError("workflow trigger fields do not match the schema")
    if value.get("type") != EVENT_TYPE or value.get("schema") != SCHEMA:
        raise ValueError("workflow trigger schema is unsupported")
    if value.get("executes") is not False:
        raise ValueError("workflow trigger evidence cannot execute")

    source = value.get("source")
    target = value.get("target")
    schedule = value.get("schedule")
    occurrence = value.get("occurrence")
    authority = value.get("authority")
    policy = value.get("policy")
    if not all(isinstance(item, Mapping) for item in (
            source, target, schedule, occurrence, authority, policy)):
        raise ValueError("workflow trigger sections must be objects")
    if set(source) != {"kind", "id", "revision"}:
        raise ValueError("source fields do not match the schema")
    if set(target) != {"ref"}:
        raise ValueError("target fields do not match the schema")
    if set(schedule) != {"kind", "timezone", "scheduled_for"}:
        raise ValueError("schedule fields do not match the schema")
    if set(occurrence) != {"key", "observed_at"}:
        raise ValueError("occurrence fields do not match the schema")
    if set(authority) != {"scheduler", "execution", "projection"}:
        raise ValueError("authority fields do not match the schema")
    if set(policy) != {"duplicates", "misfire", "catch_up"}:
        raise ValueError("policy fields do not match the schema")

    # Validate primitives without recursively rebuilding the envelope.
    if source.get("kind") not in SOURCE_KINDS:
        raise ValueError("source.kind is unsupported")
    for name, item in (("source.id", source.get("id")),
                       ("source.revision", source.get("revision")),
                       ("target.ref", target.get("ref")),
                       ("occurrence.key", occurrence.get("key")),
                       ("authority.scheduler", authority.get("scheduler"))):
        _required_text(name, item)
    if schedule.get("kind") not in SCHEDULE_KINDS:
        raise ValueError("schedule.kind is unsupported")
    _timezone(schedule.get("timezone"))
    _zoned_time("occurrence.observed_at", occurrence.get("observed_at"))
    scheduled_for = schedule.get("scheduled_for")
    if scheduled_for:
        _zoned_time("schedule.scheduled_for", scheduled_for)
    if schedule.get("kind") == "time" and not scheduled_for:
        raise ValueError("scheduled_for is required for time schedules")
    if schedule.get("kind") != "time" and scheduled_for:
        raise ValueError("scheduled_for is only valid for time schedules")
    if authority.get("execution") != "native" \
            or authority.get("projection") != "workflow_trigger":
        raise ValueError("authority declaration is unsupported")
    if policy != {"duplicates": "native_unverified",
                  "misfire": "native_unverified",
                  "catch_up": "native_unverified"}:
        raise ValueError("policy declaration is unsupported")
    identity = {
        "schema": SCHEMA,
        "source_kind": source["kind"], "source_id": source["id"],
        "source_revision": source["revision"],
        "occurrence_key": occurrence["key"], "target_ref": target["ref"],
        "schedule_kind": schedule["kind"], "timezone": schedule["timezone"],
        "scheduled_for": schedule["scheduled_for"],
        "native_owner": authority["scheduler"],
    }
    expected = _hash(identity)
    if value.get("trigger_id") != expected or value.get("idempotency_key") != expected:
        raise ValueError("workflow trigger identity does not match its content")
    return json.loads(_canonical(value))


def calendar_action_workflow_trigger(action: Mapping[str, Any], *, observed_at: str,
                                     due_kind: str) -> dict[str, Any]:
    """Project one already-due native long-term Calendar action."""
    if not isinstance(action, Mapping):
        raise TypeError("action must be an object")
    action_id = _required_text("action.id", action.get("id"))
    due_kind = "condition" if due_kind == "condition" else "time"
    definition = {
        "id": action_id,
        "title": str(action.get("title") or ""),
        "description": str(action.get("description") or ""),
        "side": str(action.get("side") or "system"),
        "goal": str(action.get("goal") or ""),
        "instructions": str(action.get("instructions") or ""),
        "when": str(action.get("when") or ""),
        "trigger": action.get("trigger") or {},
        "profile": str(action.get("profile") or ""),
    }
    from .workflow_schedule import calendar_action_schedule

    schedule = calendar_action_schedule(action, due_kind=due_kind)
    definition["schedule_id"] = schedule["schedule_id"]
    return build_workflow_trigger(
        source_kind="calendar.action", source_id=action_id,
        source_revision=_hash(definition),
        target_ref=f"sched.action:{action_id}", schedule_kind=due_kind,
        occurrence_key="one-shot", observed_at=observed_at,
        timezone_name=schedule["timezone"],
        scheduled_for=schedule["scheduled_for"],
        native_owner="vera.calendar.longterm_scheduler",
    )


def dream_schedule_workflow_trigger(trigger: Mapping[str, Any], *, observed_at: str,
                                    previous_run: str = "") -> dict[str, Any]:
    """Project one native Dream recurrence after its existing due decision."""
    if not isinstance(trigger, Mapping):
        raise TypeError("trigger must be an object")
    name = _required_text("trigger.name", trigger.get("name"))
    from .workflow_schedule import dream_trigger_schedule

    schedule = dream_trigger_schedule(trigger)
    definition = {
        "name": name,
        "hours_start": trigger.get("hours_start", 0),
        "hours_end": trigger.get("hours_end", 24),
        "min_idle_minutes": trigger.get("min_idle_minutes", 15),
        "min_interval_minutes": trigger.get("min_interval_minutes", 60),
        "require_signal": trigger.get("require_signal", 0),
        "sensors": list(trigger.get("sensors") or []),
        "sensor_params": trigger.get("sensor_params") or {},
        "pipeline": list(trigger.get("pipeline") or []),
        "mode": str(trigger.get("mode") or ""),
        "hitl": bool(trigger.get("hitl", False)),
        "timezone": schedule["timezone"],
        "schedule_id": schedule["schedule_id"],
    }
    return build_workflow_trigger(
        source_kind="dream.trigger", source_id=name,
        source_revision=_hash(definition), target_ref=f"dream.trigger:{name}",
        schedule_kind="idle_interval",
        occurrence_key=str(previous_run or "initial"), observed_at=observed_at,
        timezone_name=schedule["timezone"], native_owner="vera.dream.scheduler",
    )
