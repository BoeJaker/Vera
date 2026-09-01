"""Portable, non-executing schedule definitions and deterministic evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "vera.workflow-schedule/v1"
KINDS = frozenset({"one_shot", "condition", "interval_after_completion"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timezone(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone must be a non-empty IANA timezone")
    name = name.strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return name


def _zoned(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a zoned ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a zoned ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed


def _hour(name: str, value: Any, *, end: bool = False) -> int:
    maximum = 24 if end else 23
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def build_workflow_schedule(
    *, kind: str, timezone_name: str = "UTC", scheduled_for: str = "",
    window_start_hour: int | None = None, window_end_hour: int | None = None,
    interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Build a closed schedule definition; this function never schedules work."""
    if kind not in KINDS:
        raise ValueError("schedule kind is unsupported")
    timezone_name = _timezone(timezone_name)
    window = None
    if window_start_hour is not None or window_end_hour is not None:
        if window_start_hour is None or window_end_hour is None:
            raise ValueError("schedule window requires both start and end hours")
        window = {
            "start_hour": _hour("window.start_hour", window_start_hour),
            "end_hour": _hour("window.end_hour", window_end_hour, end=True),
        }
    recurrence = None
    if interval_seconds is not None:
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) \
                or interval_seconds < 0:
            raise ValueError("interval_seconds must be a non-negative integer")
        recurrence = {
            "kind": "interval", "anchor": "last_completed",
            "interval_seconds": interval_seconds,
        }
    if kind == "one_shot":
        parsed = _zoned("scheduled_for", scheduled_for)
        scheduled_for = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if window is not None or recurrence is not None:
            raise ValueError("one-shot schedules cannot have a window or recurrence")
    elif kind == "condition":
        if scheduled_for or window is not None or recurrence is not None:
            raise ValueError("condition schedules cannot have time semantics")
    else:
        if scheduled_for:
            raise ValueError("recurring schedules cannot have scheduled_for")
        if window is None or recurrence is None:
            raise ValueError("interval schedules require a window and recurrence")
    identity = {
        "schema": SCHEMA, "kind": kind, "timezone": timezone_name,
        "scheduled_for": scheduled_for, "window": window, "recurrence": recurrence,
    }
    value = {
        **identity, "schedule_id": _hash(identity), "executes": False,
    }
    return validate_workflow_schedule(value)


def validate_workflow_schedule(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("workflow schedule must be an object")
    allowed = {
        "schema", "schedule_id", "kind", "timezone", "scheduled_for",
        "window", "recurrence", "executes",
    }
    if set(value) != allowed or value.get("schema") != SCHEMA:
        raise ValueError("workflow schedule fields or schema are unsupported")
    # Validate nested closed shapes without recursively rebuilding.
    kind = value.get("kind")
    timezone_name = _timezone(value.get("timezone"))
    window = value.get("window")
    recurrence = value.get("recurrence")
    scheduled_for = value.get("scheduled_for")
    if window is not None:
        if not isinstance(window, Mapping) or set(window) != {"start_hour", "end_hour"}:
            raise ValueError("schedule window fields are unsupported")
        window = {
            "start_hour": _hour("window.start_hour", window["start_hour"]),
            "end_hour": _hour("window.end_hour", window["end_hour"], end=True),
        }
    if recurrence is not None:
        if not isinstance(recurrence, Mapping) or set(recurrence) != {
                "kind", "anchor", "interval_seconds"}:
            raise ValueError("schedule recurrence fields are unsupported")
        if recurrence.get("kind") != "interval" \
                or recurrence.get("anchor") != "last_completed":
            raise ValueError("schedule recurrence is unsupported")
        seconds = recurrence.get("interval_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("interval_seconds must be a non-negative integer")
        recurrence = dict(recurrence)
    if kind == "one_shot":
        canonical_time = _zoned("scheduled_for", scheduled_for).astimezone(
            timezone.utc).isoformat().replace("+00:00", "Z")
        if scheduled_for != canonical_time or window is not None or recurrence is not None:
            raise ValueError("one-shot schedule semantics are invalid")
    elif kind == "condition":
        if scheduled_for != "" or window is not None or recurrence is not None:
            raise ValueError("condition schedule semantics are invalid")
    elif kind == "interval_after_completion":
        if scheduled_for != "" or window is None or recurrence is None:
            raise ValueError("interval schedule semantics are invalid")
    else:
        raise ValueError("schedule kind is unsupported")
    identity = {
        "schema": SCHEMA, "kind": kind, "timezone": timezone_name,
        "scheduled_for": scheduled_for, "window": window, "recurrence": recurrence,
    }
    if value.get("schedule_id") != _hash(identity):
        raise ValueError("workflow schedule identity does not match its content")
    if value.get("executes") is not False:
        raise ValueError("workflow schedules cannot execute")
    return json.loads(_canonical(value))


def within_schedule_window(schedule: Mapping[str, Any], instant: datetime) -> bool:
    schedule = validate_workflow_schedule(schedule)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must include a timezone offset")
    window = schedule["window"]
    if window is None:
        return True
    hour = instant.astimezone(ZoneInfo(schedule["timezone"])).hour
    start, end = window["start_hour"], window["end_hour"]
    if start == end or (start == 0 and end == 24):
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def recurrence_due(schedule: Mapping[str, Any], *, last_completed_at: str,
                   instant: datetime) -> bool:
    schedule = validate_workflow_schedule(schedule)
    if schedule["kind"] != "interval_after_completion":
        raise ValueError("recurrence_due requires an interval schedule")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must include a timezone offset")
    if not last_completed_at:
        return True
    last = _zoned("last_completed_at", last_completed_at)
    elapsed = (instant.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
    return elapsed >= schedule["recurrence"]["interval_seconds"]


def calendar_action_schedule(action: Mapping[str, Any], *, due_kind: str) -> dict[str, Any]:
    if due_kind == "condition":
        return build_workflow_schedule(kind="condition", timezone_name="UTC")
    raw = str(action.get("when") or "")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return build_workflow_schedule(
        kind="one_shot", timezone_name="UTC", scheduled_for=parsed.isoformat())


def dream_trigger_schedule(trigger: Mapping[str, Any]) -> dict[str, Any]:
    timezone_name = str(trigger.get("timezone") or "UTC")
    return build_workflow_schedule(
        kind="interval_after_completion", timezone_name=timezone_name,
        window_start_hour=int(trigger.get("hours_start", 0)),
        window_end_hour=int(trigger.get("hours_end", 24)),
        interval_seconds=max(0, int(float(trigger.get("min_interval_minutes", 60)) * 60)),
    )


def research_iteration_schedule(iteration: Mapping[str, Any]) -> dict[str, Any]:
    """Project Research's effective after-completion recurrence."""
    if not isinstance(iteration, Mapping):
        raise TypeError("iteration must be an object")
    raw = iteration.get("interval_secs", 300)
    if isinstance(raw, bool):
        raise ValueError("iteration.interval_secs must be an integer")
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("iteration.interval_secs must be an integer") from exc
    # The native loop sleeps for at least ten seconds after each completion.
    return build_workflow_schedule(
        kind="interval_after_completion", timezone_name="UTC",
        window_start_hour=0, window_end_hour=24,
        interval_seconds=max(10, interval),
    )
