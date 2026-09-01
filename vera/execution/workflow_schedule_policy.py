"""Portable, non-executing schedule misfire and catch-up policy evidence."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .workflow_schedule import validate_workflow_schedule


POLICY_SCHEMA = "vera.workflow-schedule-policy/v1"
DECISION_SCHEMA = "vera.workflow-schedule-decision/v1"
DECISION_EVENT_TYPE = "workflow.schedule.decision"
MODES = frozenset({"fire_once", "coalesce_once", "skip"})
CLASSIFICATIONS = frozenset({
    "not_due", "within_grace", "misfire", "condition", "initial",
})
DISPOSITIONS = frozenset({
    "wait", "due_once", "coalesced_once", "skip", "native_condition",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(name: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be a zoned ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{name} must be a zoned ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_schedule_policy(*, mode: str, grace_seconds: int = 0) -> dict[str, Any]:
    """Build a closed policy definition. It classifies only; it never schedules."""
    if mode not in MODES:
        raise ValueError("schedule policy mode is unsupported")
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int) \
            or grace_seconds < 0:
        raise ValueError("grace_seconds must be a non-negative integer")
    max_catch_up = 0 if mode == "skip" else 1
    identity = {
        "schema": POLICY_SCHEMA,
        "mode": mode,
        "grace_seconds": grace_seconds,
        "max_catch_up": max_catch_up,
        "replay_effects": False,
    }
    return validate_schedule_policy({
        **identity,
        "policy_id": _hash(identity),
        "executes": False,
    })


def validate_schedule_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("schedule policy must be an object")
    allowed = {
        "schema", "policy_id", "mode", "grace_seconds", "max_catch_up",
        "replay_effects", "executes",
    }
    if set(value) != allowed or value.get("schema") != POLICY_SCHEMA:
        raise ValueError("schedule policy fields or schema are unsupported")
    mode = value.get("mode")
    grace = value.get("grace_seconds")
    if mode not in MODES:
        raise ValueError("schedule policy mode is unsupported")
    if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
        raise ValueError("grace_seconds must be a non-negative integer")
    expected_catch_up = 0 if mode == "skip" else 1
    if value.get("max_catch_up") != expected_catch_up:
        raise ValueError("max_catch_up does not match schedule policy mode")
    if value.get("replay_effects") is not False or value.get("executes") is not False:
        raise ValueError("schedule policies cannot execute or replay effects")
    identity = {
        "schema": POLICY_SCHEMA,
        "mode": mode,
        "grace_seconds": grace,
        "max_catch_up": expected_catch_up,
        "replay_effects": False,
    }
    if value.get("policy_id") != _hash(identity):
        raise ValueError("schedule policy identity does not match its content")
    return json.loads(_canonical(value))


def _decision(*, schedule: Mapping[str, Any], policy: Mapping[str, Any],
              evaluated_at: datetime, due_at: datetime | None,
              classification: str, disposition: str, lateness_seconds: int,
              missed_occurrences: int, trigger_id: str) -> dict[str, Any]:
    body = {
        "type": DECISION_EVENT_TYPE,
        "schema": DECISION_SCHEMA,
        "schedule_id": schedule["schedule_id"],
        "policy_id": policy["policy_id"],
        "trigger_id": str(trigger_id or ""),
        "evaluated_at": _iso(evaluated_at),
        "due_at": _iso(due_at) if due_at is not None else "",
        "classification": classification,
        "disposition": disposition,
        "lateness_seconds": max(0, int(lateness_seconds)),
        "missed_occurrences": max(0, int(missed_occurrences)),
        "max_catch_up": policy["max_catch_up"],
        "replay_effects": False,
        "executes": False,
    }
    return validate_schedule_decision({**body, "decision_id": _hash(body)})


def validate_schedule_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("schedule decision must be an object")
    allowed = {
        "type", "schema", "decision_id", "schedule_id", "policy_id",
        "trigger_id", "evaluated_at", "due_at", "classification",
        "disposition", "lateness_seconds", "missed_occurrences",
        "max_catch_up", "replay_effects", "executes",
    }
    if set(value) != allowed or value.get("type") != DECISION_EVENT_TYPE \
            or value.get("schema") != DECISION_SCHEMA:
        raise ValueError("schedule decision fields or schema are unsupported")
    for name in ("schedule_id", "policy_id", "classification", "disposition"):
        if not isinstance(value.get(name), str) or not value[name]:
            raise ValueError(f"{name} must be a non-empty string")
    if not value["schedule_id"].startswith("sha256:") \
            or not value["policy_id"].startswith("sha256:"):
        raise ValueError("schedule and policy identities must be sha256 references")
    trigger_id = value.get("trigger_id")
    if not isinstance(trigger_id, str) or (trigger_id and not trigger_id.startswith("sha256:")):
        raise ValueError("trigger_id must be empty or a sha256 reference")
    classification = value["classification"]
    disposition = value["disposition"]
    if classification not in CLASSIFICATIONS or disposition not in DISPOSITIONS:
        raise ValueError("schedule decision classification or disposition is unsupported")
    _instant("evaluated_at", value.get("evaluated_at"))
    if value.get("due_at"):
        _instant("due_at", value["due_at"])
    for name in ("lateness_seconds", "missed_occurrences", "max_catch_up"):
        number = value.get(name)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if value.get("replay_effects") is not False or value.get("executes") is not False:
        raise ValueError("schedule decisions cannot execute or replay effects")
    due_at = value.get("due_at")
    missed = value["missed_occurrences"]
    if classification == "not_due" and (disposition != "wait" or due_at == "" or missed != 0):
        raise ValueError("not-due schedule decision semantics are invalid")
    if classification == "condition" and (
            disposition != "native_condition" or due_at != "" or missed != 0):
        raise ValueError("condition schedule decision semantics are invalid")
    if classification == "initial" and (
            disposition not in {"due_once", "skip"} or due_at != "" or missed != 1):
        raise ValueError("initial schedule decision semantics are invalid")
    if classification in {"within_grace", "misfire"} and (
            not due_at or missed < 1 or disposition in {"wait", "native_condition"}):
        raise ValueError("due schedule decision semantics are invalid")
    if disposition == "coalesced_once" and (
            classification != "misfire" or missed < 2 or value["max_catch_up"] != 1):
        raise ValueError("coalesced schedule decision semantics are invalid")
    if disposition == "skip" and value["max_catch_up"] != 0:
        raise ValueError("skipped schedule decision semantics are invalid")
    body = {key: value[key] for key in value if key != "decision_id"}
    if value.get("decision_id") != _hash(body):
        raise ValueError("schedule decision identity does not match its content")
    return json.loads(_canonical(value))


def classify_schedule_occurrence(
    schedule: Mapping[str, Any], policy: Mapping[str, Any], *,
    evaluated_at: datetime | str, last_completed_at: str = "",
    trigger_id: str = "",
) -> dict[str, Any]:
    """Classify one observation without invoking, claiming, or replaying work."""
    schedule = validate_workflow_schedule(schedule)
    policy = validate_schedule_policy(policy)
    now = _instant("evaluated_at", evaluated_at)
    kind = schedule["kind"]

    if kind == "condition":
        return _decision(
            schedule=schedule, policy=policy, evaluated_at=now, due_at=None,
            classification="condition", disposition="native_condition",
            lateness_seconds=0, missed_occurrences=0, trigger_id=trigger_id,
        )

    if kind == "one_shot":
        due = _instant("scheduled_for", schedule["scheduled_for"])
        elapsed = (now - due).total_seconds()
        if elapsed < 0:
            classification, disposition, missed = "not_due", "wait", 0
        else:
            late = int(elapsed)
            classification = ("within_grace" if late <= policy["grace_seconds"]
                              else "misfire")
            disposition = ("skip" if policy["mode"] == "skip"
                           and classification == "misfire" else "due_once")
            missed = 1
        return _decision(
            schedule=schedule, policy=policy, evaluated_at=now, due_at=due,
            classification=classification, disposition=disposition,
            lateness_seconds=max(0, int(elapsed)), missed_occurrences=missed,
            trigger_id=trigger_id,
        )

    if not last_completed_at:
        return _decision(
            schedule=schedule, policy=policy, evaluated_at=now, due_at=None,
            classification="initial", disposition=(
                "skip" if policy["mode"] == "skip" else "due_once"),
            lateness_seconds=0, missed_occurrences=1, trigger_id=trigger_id,
        )

    last = _instant("last_completed_at", last_completed_at)
    interval = schedule["recurrence"]["interval_seconds"]
    due = last + timedelta(seconds=interval)
    elapsed = (now - due).total_seconds()
    if elapsed < 0:
        classification, disposition, missed = "not_due", "wait", 0
    else:
        missed = (1 if interval == 0 else
                  1 + math.floor(max(0.0, elapsed) / interval))
        outside_grace = elapsed > policy["grace_seconds"]
        classification = "misfire" if missed > 1 or outside_grace else "within_grace"
        if policy["mode"] == "skip" and classification == "misfire":
            disposition = "skip"
        elif policy["mode"] == "coalesce_once" and missed > 1:
            disposition = "coalesced_once"
        else:
            disposition = "due_once"
    return _decision(
        schedule=schedule, policy=policy, evaluated_at=now, due_at=due,
        classification=classification, disposition=disposition,
        lateness_seconds=max(0, int(elapsed)), missed_occurrences=missed,
        trigger_id=trigger_id,
    )


def calendar_action_policy(action: Mapping[str, Any], *, due_kind: str) -> dict[str, Any]:
    if due_kind == "condition":
        return build_schedule_policy(mode="fire_once", grace_seconds=0)
    return build_schedule_policy(mode="fire_once", grace_seconds=300)


def dream_trigger_policy(trigger: Mapping[str, Any]) -> dict[str, Any]:
    # Dream's native scheduler has always collapsed downtime to one eligible cycle.
    return build_schedule_policy(mode="coalesce_once", grace_seconds=60)


def research_iteration_policy(iteration: Mapping[str, Any]) -> dict[str, Any]:
    """Describe Research restart behavior without replaying missed effects."""
    if not isinstance(iteration, Mapping):
        raise TypeError("iteration must be an object")
    return build_schedule_policy(mode="coalesce_once", grace_seconds=0)
