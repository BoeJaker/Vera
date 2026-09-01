"""Closed, non-executing schedule lifecycle state and transition evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping


SCHEMA = "vera.workflow-schedule-lifecycle/v1"
EVENT_TYPE = "workflow.schedule.lifecycle.changed"
STATES = frozenset({"active", "paused", "cancelled", "completed", "failed"})
SOURCE_KINDS = frozenset({"calendar.action", "dream.trigger", "research.iteration"})
ALLOWED_TRANSITIONS = {
    "active": frozenset({"active", "paused", "cancelled"}),
    "paused": frozenset({"paused", "active", "cancelled"}),
    "cancelled": frozenset({"cancelled"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _zoned(name: str, value: Any) -> str:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return text


def build_schedule_lifecycle(
    *, source_kind: str, source_id: str, definition_revision: str,
    state: str, native_owner: str,
) -> dict[str, Any]:
    source_kind = _text("source_kind", source_kind)
    if source_kind not in SOURCE_KINDS:
        raise ValueError("source_kind is unsupported")
    state = _text("state", state)
    if state not in STATES:
        raise ValueError("schedule lifecycle state is unsupported")
    identity = {
        "schema": SCHEMA,
        "source_kind": source_kind,
        "source_id": _text("source_id", source_id),
        "definition_revision": _text("definition_revision", definition_revision),
        "state": state,
        "native_owner": _text("native_owner", native_owner),
        "preserves_record": True,
        "executes": False,
    }
    return validate_schedule_lifecycle({
        **identity, "lifecycle_id": _hash(identity),
    })


def validate_schedule_lifecycle(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("schedule lifecycle must be an object")
    allowed = {
        "schema", "lifecycle_id", "source_kind", "source_id",
        "definition_revision", "state", "native_owner", "preserves_record",
        "executes",
    }
    if set(value) != allowed or value.get("schema") != SCHEMA:
        raise ValueError("schedule lifecycle fields or schema are unsupported")
    if value.get("source_kind") not in SOURCE_KINDS:
        raise ValueError("source_kind is unsupported")
    if value.get("state") not in STATES:
        raise ValueError("schedule lifecycle state is unsupported")
    for name in ("source_id", "definition_revision", "native_owner"):
        _text(name, value.get(name))
    if value.get("preserves_record") is not True or value.get("executes") is not False:
        raise ValueError("schedule lifecycle must preserve records and cannot execute")
    identity = {key: value[key] for key in value if key != "lifecycle_id"}
    if value.get("lifecycle_id") != _hash(identity):
        raise ValueError("schedule lifecycle identity does not match its content")
    return json.loads(_canonical(value))


def transition_schedule_lifecycle(
    current: Mapping[str, Any], *, requested_state: str, observed_at: str,
) -> dict[str, Any]:
    current = validate_schedule_lifecycle(current)
    requested_state = _text("requested_state", requested_state)
    if requested_state not in STATES:
        raise ValueError("requested schedule lifecycle state is unsupported")
    previous = current["state"]
    if requested_state not in ALLOWED_TRANSITIONS[previous]:
        raise ValueError(f"schedule lifecycle cannot transition from {previous} to {requested_state}")
    observed_at = _zoned("observed_at", observed_at)
    updated = build_schedule_lifecycle(
        source_kind=current["source_kind"], source_id=current["source_id"],
        definition_revision=current["definition_revision"], state=requested_state,
        native_owner=current["native_owner"],
    )
    body = {
        "type": EVENT_TYPE,
        "schema": SCHEMA,
        "source_kind": current["source_kind"],
        "source_id": current["source_id"],
        "definition_revision": current["definition_revision"],
        "previous_state": previous,
        "state": requested_state,
        "transition": "no_change" if previous == requested_state else f"{previous}_to_{requested_state}",
        "observed_at": observed_at,
        "lifecycle_id": updated["lifecycle_id"],
        "native_owner": current["native_owner"],
        "preserves_record": True,
        "executes": False,
    }
    return validate_schedule_lifecycle_event({
        **body, "event_id": _hash(body), "lifecycle": updated,
    })


def validate_schedule_lifecycle_event(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("schedule lifecycle event must be an object")
    allowed = {
        "type", "schema", "event_id", "source_kind", "source_id",
        "definition_revision", "previous_state", "state", "transition",
        "observed_at", "lifecycle_id", "native_owner", "preserves_record",
        "executes", "lifecycle",
    }
    if set(value) != allowed or value.get("type") != EVENT_TYPE \
            or value.get("schema") != SCHEMA:
        raise ValueError("schedule lifecycle event fields or schema are unsupported")
    previous = value.get("previous_state")
    state = value.get("state")
    if previous not in STATES or state not in STATES \
            or state not in ALLOWED_TRANSITIONS[previous]:
        raise ValueError("schedule lifecycle event transition is unsupported")
    expected_transition = "no_change" if previous == state else f"{previous}_to_{state}"
    if value.get("transition") != expected_transition:
        raise ValueError("schedule lifecycle event transition label is invalid")
    _zoned("observed_at", value.get("observed_at"))
    lifecycle = validate_schedule_lifecycle(value.get("lifecycle"))
    for name in ("source_kind", "source_id", "definition_revision", "state",
                 "lifecycle_id", "native_owner", "preserves_record", "executes"):
        if value.get(name) != lifecycle.get(name):
            raise ValueError("schedule lifecycle event does not match its lifecycle")
    body = {key: value[key] for key in value if key not in {"event_id", "lifecycle"}}
    if value.get("event_id") != _hash(body):
        raise ValueError("schedule lifecycle event identity does not match its content")
    return json.loads(_canonical(value))


def calendar_action_lifecycle(action: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise TypeError("action must be an object")
    status = str(action.get("status") or "pending").lower()
    state = ({"cancelled": "cancelled", "paused": "paused", "done": "completed",
              "failed": "failed"}.get(status, "active"))
    definition = {
        key: action.get(key) for key in (
            "id", "side", "when", "trigger", "profile", "comms_channel",
        )
    }
    return build_schedule_lifecycle(
        source_kind="calendar.action", source_id=_text("action.id", action.get("id")),
        definition_revision=_hash(definition), state=state,
        native_owner="vera.calendar.longterm_scheduler",
    )


def dream_trigger_lifecycle(trigger: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(trigger, Mapping):
        raise TypeError("trigger must be an object")
    raw = str(trigger.get("lifecycle_state") or "").lower()
    if raw == "cancelled":
        state = "cancelled"
    else:
        state = "active" if bool(trigger.get("enabled", True)) else "paused"
    definition = {
        key: value for key, value in trigger.items()
        if key not in {
            "enabled", "lifecycle_state", "last_run", "schedule_contract",
            "schedule_policy", "schedule_error", "schedule_policy_error",
        }
    }
    return build_schedule_lifecycle(
        source_kind="dream.trigger", source_id=_text("trigger.name", trigger.get("name")),
        definition_revision=_hash(definition), state=state,
        native_owner="vera.dream.scheduler",
    )


def research_iteration_lifecycle(iteration: Mapping[str, Any]) -> dict[str, Any]:
    """Project only persisted Research lifecycle states.

    Native ``stop`` deletes the target, so a missing target is deliberately not
    invented as a preserved cancellation record.
    """
    if not isinstance(iteration, Mapping):
        raise TypeError("iteration must be an object")
    status = str(iteration.get("status") or "").lower()
    if status not in {"running", "paused"}:
        raise ValueError("research iteration status is not safely projectable")
    definition = {
        key: iteration.get(key) for key in (
            "id", "target_type", "target_id", "seed_query", "mode",
            "output_mode", "interval_secs",
        )
    }
    return build_schedule_lifecycle(
        source_kind="research.iteration",
        source_id=_text("iteration.id", iteration.get("id")),
        definition_revision=_hash(definition),
        state="active" if status == "running" else "paused",
        native_owner="vera.research.iteration_loop",
    )
