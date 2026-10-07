"""Content-free Run projection around Dream's native cycle and stage runner.

Dream remains authoritative for stage ordering, iteration, cancellation,
persistence, journals and continuations.  This adapter only projects that
lifecycle into Vera's shared Run protocol and must never affect execution.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
import hashlib
import json
from time import monotonic
from typing import Any, Mapping
from uuid import uuid4

from .run_projection import SHADOW_RUNS
from .run_protocol import Run, RunError, RunStatus, TERMINAL_STATUSES


def _text(value: Any) -> str:
    return str(value or "").strip()


def _workflow_identity(stages: list[str], workflow_plan: Mapping[str, Any] | None) -> str:
    if workflow_plan:
        content_hash = _text(workflow_plan.get("content_hash"))
        if content_hash:
            return f"dream-workflow:{content_hash}"
    encoded = json.dumps(stages, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "dream-pipeline:" + hashlib.sha256(encoded).hexdigest()


class DreamRunProjection:
    """Failure-isolated parent/stage projection for one native Dream cycle."""

    def __init__(self, *, cycle_id: str, registry=None) -> None:
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.parent = Run(
            id=str(uuid4()), kind="vera.dream.cycle",
            session_id=f"dream:{_text(cycle_id)}", trace_id=_text(cycle_id),
            workflow_id="dream-pipeline:pending",
        )
        self.children: dict[str, Run] = {}
        self.sequence = 0

    @property
    def terminal(self) -> bool:
        return self.parent.status in TERMINAL_STATUSES

    def _safe(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            return None

    def _record(self, run: Run, event) -> None:
        self.registry.record(run, event)

    def _start(self) -> None:
        event = self.parent.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"execution_owner": "dream.native-stage-runner"},
        )
        self._record(self.parent, event)

    def configure(self, *, stages: list[str],
                  workflow_plan: Mapping[str, Any] | None = None,
                  schedule_id: str = "") -> None:
        self._safe(self._configure, list(stages), workflow_plan, schedule_id)

    def _configure(self, stages: list[str], workflow_plan: Mapping[str, Any] | None,
                   schedule_id: str) -> None:
        if self.terminal:
            return
        workflow_id = _workflow_identity(stages, workflow_plan)
        if self.parent.status == RunStatus.CREATED:
            # Run identity is immutable once registered in the durable journal.
            # Resolve the workflow before the first event rather than mutating
            # a registered "pending" identity.
            self.parent.workflow_id = workflow_id
            self._start()
        elif self.parent.workflow_id != workflow_id:
            return
        event = self.parent.record_event(
            "run.workflow.bound",
            payload={"workflow_id": self.parent.workflow_id,
                     "stage_count": len(stages),
                     "portable_definition": bool(workflow_plan)},
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)
        if _text(schedule_id):
            event = self.parent.record_event(
                "run.schedule.bound",
                payload={"schedule_id": _text(schedule_id),
                         "native_owner": "vera.dream.scheduler"},
                causation_id=self.parent.events[-1].id,
            )
            self._record(self.parent, event)

    def stage_started(self, *, stage: str, index: int | None,
                      iteration: int | None) -> tuple[str, float] | None:
        return self._safe(self._stage_started, stage, index, iteration)

    def _stage_started(self, stage: str, index: int | None,
                       iteration: int | None) -> tuple[str, float] | None:
        if self.terminal:
            return None
        if self.parent.status == RunStatus.CREATED:
            self._configure([], None, "")
        self.sequence += 1
        child = Run(
            id=str(uuid4()), kind="vera.dream.stage",
            parent_run_id=self.parent.id, workflow_id=self.parent.workflow_id,
            task_id=f"stage:{self.sequence}", session_id=self.parent.session_id,
            trace_id=self.parent.trace_id,
        )
        self.children[child.id] = child
        event = child.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"capability": _text(stage), "stage_index": index,
                     "iteration": iteration},
            causation_id=self.parent.events[-1].id,
        )
        self._record(child, event)
        return child.id, monotonic()

    def stage_finished(self, handle: tuple[str, float] | None, *, ok: bool,
                       cancelled: bool = False) -> None:
        self._safe(self._stage_finished, handle, ok, cancelled)

    def _stage_finished(self, handle: tuple[str, float] | None, ok: bool,
                        cancelled: bool) -> None:
        if not handle:
            return
        child = self.children.get(handle[0])
        if not child or child.status in TERMINAL_STATUSES:
            return
        elapsed_ms = max(0, round((monotonic() - handle[1]) * 1000))
        target = (RunStatus.CANCELLED if cancelled else
                  RunStatus.COMPLETED if ok else RunStatus.FAILED)
        event_type = ("run.cancelled" if cancelled else
                      "run.completed" if ok else "run.failed")
        if target == RunStatus.FAILED:
            child.error = RunError(code="dream_stage_failed",
                                   message="native Dream stage reported failure")
        event = child.transition(
            target, event_type=event_type,
            payload={"elapsed_ms": elapsed_ms, "ok": bool(ok)},
            causation_id=child.events[-1].id,
        )
        self._record(child, event)

    def finish(self, *, cancelled: bool = False, error_type: str = "",
               early_exit: bool = False) -> None:
        self._safe(self._finish, cancelled, error_type, early_exit)

    def _finish(self, cancelled: bool, error_type: str, early_exit: bool) -> None:
        if self.terminal:
            return
        if self.parent.status == RunStatus.CREATED:
            self._start()
        for child in self.children.values():
            if child.status not in TERMINAL_STATUSES:
                event = child.transition(
                    RunStatus.CANCELLED, event_type="run.cancelled",
                    payload={"reason_code": "parent_terminal_without_stage_result"},
                    causation_id=child.events[-1].id,
                )
                self._record(child, event)
        target = (RunStatus.CANCELLED if cancelled else
                  RunStatus.FAILED if error_type else RunStatus.COMPLETED)
        event_type = ("run.cancelled" if cancelled else
                      "run.failed" if error_type else "run.completed")
        if target == RunStatus.FAILED:
            self.parent.error = RunError(code=error_type or "dream_cycle_failed",
                                         message="native Dream cycle failed")
        event = self.parent.transition(
            target, event_type=event_type,
            payload={"observed_stages": len(self.children),
                     "early_exit": bool(early_exit)},
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)


_CURRENT: ContextVar[DreamRunProjection | None] = ContextVar(
    "vera_dream_run_projection", default=None)


def enter_dream_run_boundary(*, cycle_id: str, registry=None
                             ) -> tuple[DreamRunProjection | None, Token | None]:
    try:
        projection = DreamRunProjection(cycle_id=cycle_id, registry=registry)
        return projection, _CURRENT.set(projection)
    except Exception:
        return None, None


def configure_dream_run(*, stages: list[str],
                        workflow_plan: Mapping[str, Any] | None = None,
                        schedule_id: str = "") -> None:
    projection = _CURRENT.get()
    if projection:
        projection.configure(
            stages=stages, workflow_plan=workflow_plan, schedule_id=schedule_id)


def start_dream_stage(*, stage: str, index: int | None,
                      iteration: int | None) -> tuple[str, float] | None:
    projection = _CURRENT.get()
    return projection.stage_started(stage=stage, index=index, iteration=iteration) if projection else None


def finish_dream_stage(handle: tuple[str, float] | None, *, ok: bool,
                       cancelled: bool = False) -> None:
    projection = _CURRENT.get()
    if projection:
        projection.stage_finished(handle, ok=ok, cancelled=cancelled)


def exit_dream_run_boundary(projection: DreamRunProjection | None,
                            token: Token | None, *, result: Any = None,
                            cancelled: bool = False,
                            error_type: str = "") -> None:
    try:
        if projection:
            if isinstance(result, Mapping):
                cancelled = cancelled or bool(result.get("cancelled"))
                early_exit = bool(result.get("early_exit"))
            else:
                early_exit = False
            projection.finish(cancelled=cancelled, error_type=_text(error_type),
                              early_exit=early_exit)
    finally:
        if token is not None:
            try:
                _CURRENT.reset(token)
            except Exception:
                pass
