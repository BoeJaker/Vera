"""Content-free shared Run projection for native Operator execution.

The browser loop and its Redis cancel/history records remain authoritative.
This observer only gives Activity and other read-only UIs a portable Run view.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..execution.run_protocol import ArtifactRef, Run, RunError, RunStatus
from ..execution.run_projection import SHADOW_RUNS, ShadowRunRegistry


SCHEMA = "vera.operator-run-projection/v1"
_ACTIVE: dict[str, "OperatorRunProjection"] = {}
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SUCCESS_REASONS = {"done"}
_CANCEL_REASONS = {"cancelled"}
_TIMEOUT_REASONS = {"time_budget"}


def _safe_id(value: Any, default: str = "unknown") -> str:
    candidate = str(value or "").strip()
    return candidate if _OPAQUE_ID.fullmatch(candidate) else default


class OperatorRunProjection:
    def __init__(self, run_id: str, *, session_id: str = "", target: str = "",
                 registry: ShadowRunRegistry | None = None) -> None:
        if not _OPAQUE_ID.fullmatch(str(run_id or "")):
            raise ValueError("run_id must be an opaque identifier")
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.parent = Run(id=run_id, kind="operator.run",
                          session_id=_safe_id(session_id, ""), trace_id=run_id,
                          policy={"execution_authority": "native_operator",
                                  "content_policy": "metadata_only"})
        self.children: dict[int, Run] = {}
        self._transition(self.parent, RunStatus.QUEUED, "operator.run.queued",
                         {"target_kind": _safe_id(target)})
        self._transition(self.parent, RunStatus.RUNNING, "operator.run.started")

    def _transition(self, run: Run, status: RunStatus, event_type: str,
                    payload: Mapping[str, Any] | None = None) -> None:
        event = run.transition(status, event_type=event_type, payload=payload)
        self.registry.record(run, event)

    def step(self, value: Mapping[str, Any]) -> None:
        try:
            index = int(value.get("i"))
        except (TypeError, ValueError) as exc:
            raise ValueError("operator step index must be a positive integer") from exc
        if index < 1 or index > 10_000:
            raise ValueError("operator step index must be between 1 and 10000")
        if index in self.children:
            raise ValueError("operator step was already projected")
        phase = _safe_id(value.get("phase"), "unknown")
        action = _safe_id(value.get("action"), "none")
        child = Run(id=f"{self.parent.id}:step:{index}", kind="operator.step",
                    parent_run_id=self.parent.id, session_id=self.parent.session_id,
                    trace_id=self.parent.trace_id, task_id=str(index),
                    policy=self.parent.policy)
        self.children[index] = child
        self._transition(child, RunStatus.QUEUED, "operator.step.queued",
                         {"step_index": index, "phase": phase, "action": action})
        self._transition(child, RunStatus.RUNNING, "operator.step.started")
        screenshot = str(value.get("screenshot") or "").strip()
        if screenshot.startswith("/operator/artifact?path="):
            child.artifacts.append(ArtifactRef(
                id=f"{child.id}:screenshot", kind="operator.screenshot",
                uri=screenshot, media_type="image/png"))
        failed = bool(value.get("error")) or phase in {
            "blocked", "observe_error", "think_error", "invalid"}
        if failed:
            child.error = RunError("operator_step_failed", "Native Operator step failed")
            target = RunStatus.FAILED
        elif phase == "cancelled":
            target = RunStatus.CANCELLED
        else:
            target = RunStatus.COMPLETED
        self._transition(child, target, f"operator.step.{target.value}",
                         {"step_index": index,
                          "artifact_ids": [item.id for item in child.artifacts]})
        event = self.parent.record_event(
            "operator.run.progress", payload={"last_step_index": index})
        self.registry.record(self.parent, event)

    def finish(self, reason: str) -> None:
        if self.parent.status != RunStatus.RUNNING:
            raise ValueError("operator run is not running")
        normalized = str(reason or "unknown").strip().lower()
        if normalized in _SUCCESS_REASONS:
            target = RunStatus.COMPLETED
        elif normalized in _CANCEL_REASONS:
            target = RunStatus.CANCELLED
        elif normalized in _TIMEOUT_REASONS:
            target = RunStatus.TIMED_OUT
        else:
            target = RunStatus.FAILED
            self.parent.error = RunError(
                "operator_run_incomplete", "Native Operator run did not complete its goal",
                retryable=False, details={"reason": _safe_id(normalized)})
        self._transition(self.parent, target, f"operator.run.{target.value}",
                         {"native_reason": _safe_id(normalized),
                          "progress": 1.0 if target == RunStatus.COMPLETED
                          else self.parent.progress})

    def to_dict(self) -> dict[str, Any]:
        value = self.registry.get(self.parent.id)
        if value is None:
            raise ValueError("projected run is missing from the shared registry")
        return {"schema": SCHEMA, "execution_authority": "native_operator",
                "content_policy": "metadata_only", **value}


def observe(run_id: str, event: Mapping[str, Any], *,
            registry: ShadowRunRegistry | None = None) -> None:
    """Project one legacy Operator event without gaining execution authority."""
    event_type = str(event.get("type") or "")
    stage = str(event.get("stage") or "")
    if event_type == "operator.run" and stage == "start":
        _ACTIVE[run_id] = OperatorRunProjection(
            run_id, session_id=str(event.get("session_id") or ""),
            target=str(event.get("target") or ""), registry=registry)
    elif event_type == "operator.step" and run_id in _ACTIVE:
        _ACTIVE[run_id].step(event)
    elif event_type == "operator.run" and stage == "done" and run_id in _ACTIVE:
        _ACTIVE[run_id].finish(str(event.get("reason") or ""))
        _ACTIVE.pop(run_id, None)


def drop(run_id: str) -> None:
    _ACTIVE.pop(run_id, None)
