"""Best-effort Memory observation for completed native workflow executions."""

from __future__ import annotations

import importlib
import logging
import sys


log = logging.getLogger(__name__)


def _memory_hooks():
    """Resolve the hook under either supported Vera package topology."""
    for name in (
        "Vera.vera.fabric.memory_hooks",
        "vera.fabric.memory_hooks",
        "memory_hooks",
    ):
        module = sys.modules.get(name)
        if module is not None:
            return module
    for name in ("Vera.vera.fabric.memory_hooks", "vera.fabric.memory_hooks"):
        try:
            return importlib.import_module(name)
        except (ImportError, ModuleNotFoundError):
            continue
    return None


async def observe_completed_workflow(
    *,
    session_id: str,
    dag: list,
    state: dict,
    result: dict | None,
    trigger: str,
    agent_name: str = "",
) -> bool:
    """Record a completed workflow without becoming part of its authority.

    Memory observation is intentionally fail-open: a storage or import failure
    must not turn an already successful workflow into a failed execution.
    """
    if not dag:
        return False
    try:
        hooks = _memory_hooks()
        recorder = getattr(hooks, "record_dag_execution", None) if hooks else None
        if recorder is None:
            return False
        await recorder(
            session_id=session_id or "",
            dag=dag,
            state=state or {},
            result=result or {},
            agent_name=agent_name or "",
            trigger=trigger,
        )
        return True
    except Exception as exc:
        log.debug("workflow memory observation failed: %s", exc)
        return False
