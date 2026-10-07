"""Content-free Run projection for eligible native schedule executions.

The Calendar, Dream and Research schedulers retain all timing, due-decision,
retry, persistence and effect authority.  This module observes only executions
that already started; it never evaluates or dispatches a schedule.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .run_projection import SHADOW_RUNS
from .run_protocol import Run, RunError, RunStatus, TERMINAL_STATUSES


def _text(value: Any) -> str:
    return str(value or "").strip()


class ScheduledRunProjection:
    """Failure-isolated projection for one native scheduled occurrence."""

    def __init__(self, *, source: str, native_id: str, schedule_id: str,
                 occurrence: int | None = None, registry=None) -> None:
        if not all((_text(source), _text(native_id), _text(schedule_id))):
            raise ValueError("source, native_id and schedule_id are required")
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.run = Run(
            id=str(uuid4()), kind=f"vera.schedule.{_text(source)}",
            session_id=f"schedule:{_text(source)}:{_text(native_id)}",
            trace_id=f"schedule:{_text(source)}:{_text(native_id)}",
            workflow_id=f"schedule:{_text(schedule_id)}",
            task_id=(f"occurrence:{occurrence}" if occurrence is not None else ""),
        )
        self._safe(self._start, occurrence)

    @property
    def terminal(self) -> bool:
        return self.run.status in TERMINAL_STATUSES

    def _safe(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            return None

    def _record(self, event) -> None:
        self.registry.record(self.run, event)

    def _start(self, occurrence: int | None) -> None:
        event = self.run.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"source": self.run.kind.removeprefix("vera.schedule."),
                     "schedule_id": self.run.workflow_id.removeprefix("schedule:"),
                     "occurrence": occurrence},
        )
        self._record(event)

    def finish(self, *, cancelled: bool = False, error_type: str = "") -> None:
        self._safe(self._finish, cancelled, _text(error_type))

    def _finish(self, cancelled: bool, error_type: str) -> None:
        if self.terminal:
            return
        target = (RunStatus.CANCELLED if cancelled else
                  RunStatus.FAILED if error_type else RunStatus.COMPLETED)
        event_type = ("run.cancelled" if cancelled else
                      "run.failed" if error_type else "run.completed")
        if error_type:
            self.run.error = RunError(
                code=error_type, message="native scheduled execution failed")
        event = self.run.transition(
            target, event_type=event_type,
            payload={"source": self.run.kind.removeprefix("vera.schedule.")},
            causation_id=self.run.events[-1].id,
        )
        self._record(event)


def start_scheduled_run(*, source: str, native_id: str, schedule_id: str,
                        occurrence: int | None = None, registry=None
                        ) -> ScheduledRunProjection | None:
    try:
        return ScheduledRunProjection(
            source=source, native_id=native_id, schedule_id=schedule_id,
            occurrence=occurrence, registry=registry)
    except Exception:
        return None


def finish_scheduled_run(projection: ScheduledRunProjection | None, *,
                         cancelled: bool = False, error_type: str = "") -> None:
    if projection is not None:
        projection.finish(cancelled=cancelled, error_type=error_type)
