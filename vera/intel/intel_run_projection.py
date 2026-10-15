"""Content-free Run projection for Intel's native idle-queue jobs.

Intel and the idle queue retain scheduling, dispatch, deduplication,
pre-emption, retry, persistence, result and effect authority.  This module
observes one invocation of the already-selected native handler; projection
failure can never affect that handler.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..execution.run_projection import SHADOW_RUNS
from ..execution.run_protocol import Run, RunError, RunStatus, TERMINAL_STATUSES


INTEL_JOB_MODES = frozenset({
    "topic", "sweeps", "first", "files", "advise", "research", "speak",
    "brief", "night",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


class IntelJobRunProjection:
    """Failure-isolated projection for one native Intel job invocation."""

    def __init__(self, *, job_id: str, mode: str, attempt: int = 1,
                 registry=None) -> None:
        self.registry = registry if registry is not None else SHADOW_RUNS
        requested_mode = _text(mode)
        self.mode = requested_mode if requested_mode in INTEL_JOB_MODES else "unknown"
        self.native_id = _text(job_id) or f"inline-{uuid4()}"
        try:
            normalized_attempt = max(1, int(attempt or 1))
        except (TypeError, ValueError):
            normalized_attempt = 1
        self.run = Run(
            id=str(uuid4()),
            kind=f"vera.intel.{self.mode}",
            workflow_id=f"intel.idle.{self.mode}",
            task_id=f"invocation:{normalized_attempt}",
            session_id=f"idle-queue:{self.native_id}",
            trace_id=f"intel:{self.native_id}",
            attempt=normalized_attempt,
            retry_owner="idle_queue",
            policy={"execution_owner": "intel.native", "authority": "shadow"},
        )
        self._safe(self._start)

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

    def _start(self) -> None:
        event = self.run.transition(
            RunStatus.RUNNING,
            event_type="run.started",
            payload={
                "mode": self.mode,
                "native_job_id": self.native_id,
                "execution_owner": "intel.native",
            },
        )
        self._record(event)

    def finish(self, *, result: Any = None, cancelled: bool = False,
               error_type: str = "") -> None:
        self._safe(self._finish, result, cancelled, _text(error_type))

    def _finish(self, result: Any, cancelled: bool, error_type: str) -> None:
        if self.terminal:
            return
        value = result if isinstance(result, Mapping) else {}
        yielded = bool(_text(value.get("yielded")))
        skipped = bool(_text(value.get("skipped")))
        reported_error = bool(value.get("error") or value.get("ok") is False)
        if cancelled or yielded:
            target, event_type = RunStatus.CANCELLED, "run.cancelled"
            reason_code = "native_preempted" if yielded else "native_cancelled"
        elif error_type or reported_error:
            target, event_type = RunStatus.FAILED, "run.failed"
            reason_code = error_type or "native_result_error"
            self.run.error = RunError(
                code=reason_code,
                message="native Intel job reported failure",
            )
        elif skipped:
            target, event_type = RunStatus.SKIPPED, "run.skipped"
            reason_code = "native_skipped"
        else:
            target, event_type = RunStatus.COMPLETED, "run.completed"
            reason_code = "native_completed"
        event = self.run.transition(
            target,
            event_type=event_type,
            payload={"mode": self.mode, "reason_code": reason_code},
            causation_id=self.run.events[-1].id,
        )
        self._record(event)


def start_intel_job_run(*, job_id: str, mode: str, attempt: int = 1,
                        registry=None) -> IntelJobRunProjection | None:
    try:
        return IntelJobRunProjection(
            job_id=job_id, mode=mode, attempt=attempt, registry=registry)
    except Exception:
        return None


def finish_intel_job_run(projection: IntelJobRunProjection | None, *,
                         result: Any = None, cancelled: bool = False,
                         error_type: str = "") -> None:
    if projection is not None:
        projection.finish(
            result=result, cancelled=cancelled, error_type=error_type)
