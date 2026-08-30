"""Behavior-preserving shadow instrumentation for existing execution engines."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from .run_protocol import PROTOCOL_VERSION, Run, RunStatus
from .run_projection import DagRunObserver, SHADOW_RUNS


Emitter = Callable[[dict[str, Any]], Awaitable[None]]


async def execute_dag_with_run_shadow(*, executor, graph: list, state: dict,
                                      trace_id: str, emit: Emitter,
                                      session_id: str = "",
                                      workflow_id: str = "") -> dict:
    """Execute the native DAG unchanged while best-effort shadow events observe it."""
    run = Run(id=str(uuid4()), kind="vera.dag", trace_id=trace_id,
              workflow_id=workflow_id or trace_id, session_id=session_id)

    async def shadow(status: RunStatus, event_type: str,
                     payload: dict[str, Any] | None = None) -> None:
        event = run.transition(status, event_type=event_type, payload=payload)
        try:
            SHADOW_RUNS.record(run, event)
            await emit({"type": "run.event", "protocol": PROTOCOL_VERSION,
                        "run": run.to_dict(include_events=False),
                        "event": event.to_dict()})
        except Exception:
            # Shadow observability must never alter native DAG behavior.
            return

    await shadow(RunStatus.RUNNING, "run.started", {"node_count": len(graph)})
    try:
        observer = DagRunObserver(parent=run, graph=graph, emit=emit)
        result = await executor(graph, state, trace_id, observer)
    except asyncio.CancelledError:
        await shadow(RunStatus.CANCELLED, "run.cancelled")
        raise
    except TimeoutError:
        await shadow(RunStatus.TIMED_OUT, "run.timed_out")
        raise
    except BaseException as exc:
        await shadow(RunStatus.FAILED, "run.failed",
                     {"error_type": type(exc).__name__})
        raise
    await shadow(RunStatus.COMPLETED, "run.completed",
                 {"result_keys": sorted(str(key) for key in result)})
    return result
