"""Content-free shadow Run projection for native agent-loop events.

The native agent loop and its SSE/event schemas remain authoritative.  This
module observes lifecycle metadata only so Chat, IDE, Activity and graph views
can correlate the same execution through Vera's shared Run protocol.
"""

from __future__ import annotations

from collections import defaultdict, deque
from contextvars import ContextVar, Token
from typing import Any, Mapping
from uuid import uuid4

from .run_protocol import Run, RunError, RunEvent, RunStatus, TERMINAL_STATUSES
from .run_projection import SHADOW_RUNS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AgentLoopRunProjection:
    """Failure-isolated parent/tool Run projection for one loop session."""

    def __init__(self, *, session_id: str, engine: str = "", profile: str = "",
                 registry=None) -> None:
        if not _text(session_id):
            raise ValueError("session_id is required")
        self.registry = registry if registry is not None else SHADOW_RUNS
        self.parent = Run(
            id=str(uuid4()), kind="vera.agent_loop", session_id=_text(session_id),
            trace_id=_text(session_id), workflow_id=f"agent-loop:{_text(engine) or 'unknown'}",
        )
        self.engine = _text(engine) or "unknown"
        self.profile = _text(profile)
        self.children: dict[str, Run] = {}
        self.active: dict[tuple[int, int, str], deque[str]] = defaultdict(deque)
        self.tool_sequence = 0
        self._safe(self._start)

    @property
    def run_id(self) -> str:
        return self.parent.id

    @property
    def terminal(self) -> bool:
        return self.parent.status in TERMINAL_STATUSES

    def _safe(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            # Observability must never change native loop behavior.
            return None

    def _record(self, run: Run, event: RunEvent) -> None:
        self.registry.record(run, event)

    def _start(self) -> None:
        event = self.parent.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"engine": self.engine, "profile": self.profile},
        )
        self._record(self.parent, event)

    @staticmethod
    def _event_key(event: Mapping[str, Any]) -> tuple[int, int, str]:
        return (_integer(event.get("step_id"), -1),
                _integer(event.get("cycle"), -1), _text(event.get("tool")))

    def observe(self, event: Mapping[str, Any]) -> None:
        self._safe(self._observe, dict(event or {}))

    def _observe(self, event: dict[str, Any]) -> None:
        if self.terminal:
            return
        event_type = _text(event.get("type"))
        if not event_type.startswith("agent_loop"):
            return
        if event_type.endswith(".tool_call"):
            self._tool_started(event)
        elif event_type.endswith(".tool_done"):
            self._tool_finished(event)
        elif event_type.endswith(".hitl_request") or event_type.endswith(".clarify_request"):
            self._approval_pending(event_type)
        elif event_type.endswith(".hitl_resolved") or event_type.endswith(".clarify_resolved"):
            self._approval_resumed(event_type)
        elif event_type.endswith(".toolkit"):
            toolkit = event.get("toolkit")
            self._observe_parent("run.toolkit.available", {
                "capability_count": len(toolkit) if isinstance(toolkit, list) else 0,
            })
        elif event_type.endswith(".plan") or event_type.endswith(".master_plan"):
            steps = event.get("steps") or event.get("pieces")
            self._observe_parent("run.plan.observed", {
                "step_count": len(steps) if isinstance(steps, list) else 0,
            })
        elif event_type.endswith(".done"):
            if bool(event.get("cancelled")):
                self.finish(cancelled=True)
            elif event.get("error"):
                self.finish(error_type="native_loop_error")
            else:
                self.finish()

    def _observe_parent(self, event_type: str, payload: dict[str, Any]) -> None:
        event = self.parent.record_event(
            event_type, payload=payload,
            causation_id=self.parent.events[-1].id if self.parent.events else "",
        )
        self._record(self.parent, event)

    def _tool_started(self, native: Mapping[str, Any]) -> None:
        tool = _text(native.get("tool")) or "[unknown]"
        step_id, cycle, _ = self._event_key(native)
        self.tool_sequence += 1
        child = Run(
            id=str(uuid4()), kind="vera.agent_loop.tool",
            parent_run_id=self.parent.id, workflow_id=self.parent.workflow_id,
            task_id=(f"step:{step_id}" if step_id >= 0 else f"cycle:{cycle}"
                     if cycle >= 0 else f"tool:{self.tool_sequence}"),
            session_id=self.parent.session_id, trace_id=self.parent.trace_id,
        )
        self.children[child.id] = child
        self.active[(step_id, cycle, tool)].append(child.id)
        event = child.transition(
            RunStatus.RUNNING, event_type="run.started",
            payload={"capability": tool, "step_id": step_id, "cycle": cycle},
            causation_id=self.parent.events[-1].id,
        )
        self._record(child, event)

    def _tool_finished(self, native: Mapping[str, Any]) -> None:
        key = self._event_key(native)
        queue = self.active.get(key)
        if not queue:
            return
        child_id = queue.popleft()
        if not queue:
            self.active.pop(key, None)
        child = self.children.get(child_id)
        if not child or child.status != RunStatus.RUNNING:
            return
        ok = bool(native.get("ok"))
        elapsed = max(0, _integer(native.get("elapsed_ms"), 0))
        payload = {"capability": key[2] or "[unknown]", "step_id": key[0],
                   "cycle": key[1], "elapsed_ms": elapsed, "ok": ok}
        if ok:
            event = child.transition(
                RunStatus.COMPLETED, event_type="run.completed", payload=payload,
                causation_id=child.events[-1].id,
            )
        else:
            child.error = RunError(code="capability_failed",
                                   message="capability reported failure")
            event = child.transition(
                RunStatus.FAILED, event_type="run.failed", payload=payload,
                causation_id=child.events[-1].id,
            )
        self._record(child, event)
        self._record_progress(event.id)

    def _record_progress(self, causation_id: str) -> None:
        finished = sum(child.status in TERMINAL_STATUSES
                       for child in self.children.values())
        event = self.parent.record_event(
            "run.progress", payload={"completed_tools": finished,
                                     "observed_tools": len(self.children)},
            causation_id=causation_id,
        )
        self._record(self.parent, event)

    def _approval_pending(self, source_type: str) -> None:
        if self.parent.status != RunStatus.RUNNING:
            return
        event = self.parent.transition(
            RunStatus.APPROVAL_PENDING, event_type="run.approval.pending",
            payload={"source_event": source_type},
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)

    def _approval_resumed(self, source_type: str) -> None:
        if self.parent.status != RunStatus.APPROVAL_PENDING:
            return
        event = self.parent.transition(
            RunStatus.RUNNING, event_type="run.approval.resumed",
            payload={"source_event": source_type},
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)

    def finish(self, *, cancelled: bool = False, timed_out: bool = False,
               error_type: str = "") -> None:
        self._safe(self._finish, cancelled=cancelled, timed_out=timed_out,
                   error_type=error_type)

    def _finish(self, *, cancelled: bool, timed_out: bool, error_type: str) -> None:
        if self.terminal:
            return
        if self.parent.status == RunStatus.APPROVAL_PENDING:
            resumed = self.parent.transition(
                RunStatus.RUNNING, event_type="run.approval.abandoned",
                payload={"reason_code": "parent_terminal"},
                causation_id=self.parent.events[-1].id,
            )
            self._record(self.parent, resumed)
        target = (RunStatus.CANCELLED if cancelled else RunStatus.TIMED_OUT
                  if timed_out else RunStatus.FAILED if error_type else RunStatus.COMPLETED)
        reason = ("cancelled" if cancelled else "timed_out" if timed_out
                  else "failed" if error_type else "completed")
        for child in self.children.values():
            if child.status in TERMINAL_STATUSES:
                continue
            if child.status == RunStatus.APPROVAL_PENDING:
                resumed = child.transition(
                    RunStatus.RUNNING, event_type="run.approval.abandoned",
                    payload={"reason_code": "parent_terminal"},
                    causation_id=child.events[-1].id,
                )
                self._record(child, resumed)
            event = child.transition(
                RunStatus.CANCELLED, event_type="run.cancelled",
                payload={"reason_code": "parent_terminal_without_tool_done"},
                causation_id=child.events[-1].id,
            )
            self._record(child, event)
        if target == RunStatus.FAILED:
            self.parent.error = RunError(code=error_type or "agent_loop_failed",
                                         message="native agent loop failed")
        event = self.parent.transition(
            target, event_type=f"run.{reason}",
            payload={"observed_tools": len(self.children)},
            causation_id=self.parent.events[-1].id,
        )
        self._record(self.parent, event)


_ACTIVE: dict[str, AgentLoopRunProjection] = {}
_CURRENT: ContextVar[AgentLoopRunProjection | None] = ContextVar(
    "vera_agent_loop_run_projection", default=None)


def bind_agent_loop_projection(
        projection: AgentLoopRunProjection | None) -> Token:
    """Bind an exact projection to this loop task and inherited child tasks."""
    return _CURRENT.set(projection)


def reset_agent_loop_projection(token: Token | None) -> None:
    if token is None:
        return
    try:
        _CURRENT.reset(token)
    except Exception:
        return


def start_agent_loop_projection(*, session_id: str, engine: str = "",
                                profile: str = "", registry=None
                                ) -> AgentLoopRunProjection | None:
    """Start one projection; supersede an older run using the same session."""
    sid = _text(session_id)
    if not sid:
        return None
    try:
        previous = _ACTIVE.get(sid)
        if previous and not previous.terminal:
            previous.finish(cancelled=True)
        projection = AgentLoopRunProjection(
            session_id=sid, engine=engine, profile=profile, registry=registry)
        _ACTIVE[sid] = projection
        return projection
    except Exception:
        return None


def observe_agent_loop_event(event: Mapping[str, Any]) -> None:
    """Observe one already-stamped native event without mutating it."""
    try:
        event_type = _text(event.get("type"))
        if not event_type.startswith("agent_loop"):
            return
        sid = _text(event.get("session_id") or event.get("sid"))
        # Context identity outranks session lookup. A superseded task retains
        # its cancelled projection in its copied context and cannot write into
        # the replacement run that now owns the same chat session.
        projection = _CURRENT.get() or _ACTIVE.get(sid)
        if projection:
            projection.observe(event)
    except Exception:
        return


def finish_agent_loop_projection(projection: AgentLoopRunProjection | None, *,
                                 result: Any = None, cancelled: bool = False,
                                 timed_out: bool = False,
                                 error_type: str = "") -> None:
    """Finish an exact projection handle; never inspect result content."""
    if projection is None:
        return
    try:
        if isinstance(result, Mapping):
            cancelled = cancelled or bool(result.get("cancelled"))
            error_type = error_type or ("native_loop_error" if result.get("error") else "")
        projection.finish(cancelled=cancelled, timed_out=timed_out,
                          error_type=_text(error_type))
        if _ACTIVE.get(projection.parent.session_id) is projection:
            _ACTIVE.pop(projection.parent.session_id, None)
    except Exception:
        return
