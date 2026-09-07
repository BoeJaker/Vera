"""Embedded runtime adapter for exact, per-action Workflow IR execution.

The adapter deliberately delegates execution to Vera's existing call-tool
function.  Workflow IR owns the portable action boundary and provenance; the
native caller continues to own admission, argument handling, events, awaiting,
and capability execution.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from .dag_workflow_execution import prepare_stepwise_dag_action


SCHEMA = "vera.embedded-workflow-runtime/v1"
CallTool = Callable[..., Awaitable[dict[str, Any]]]


def enabled() -> bool:
    """Return the reversible cutover switch (enabled by default)."""
    return str(os.getenv("VERA_AGENT_WORKFLOW_RUNTIME", "1")).strip().lower() \
        not in {"0", "false", "no", "off"}


class EmbeddedWorkflowRuntimeAdapter:
    """Execute one exact Workflow IR action through the native caller."""

    def __init__(self, call_tool: CallTool) -> None:
        if not callable(call_tool):
            raise TypeError("call_tool must be callable")
        self._call_tool = call_tool

    async def run(
        self,
        cap_name: str,
        args: Any,
        **call_options: Any,
    ) -> dict[str, Any]:
        prepared = prepare_stepwise_dag_action(
            cap_name, "result", include_workflow_ir=True,
        )
        result = await self._call_tool(
            prepared["cap"], args, **call_options,
        )
        if not isinstance(result, dict):
            raise TypeError("native call-tool result must be an object")
        # Add only content-free execution provenance. Existing ok/result/error
        # fields remain byte-for-byte owned by the native caller.
        return {
            **result,
            "runtime_execution": {
                "schema": SCHEMA,
                "runtime_id": "vera.embedded-capability",
                "execution_authority": "native_call_tool",
                "workflow_ir": prepared["workflow_ir"],
            },
        }


def wrap_call_tool(call_tool: CallTool) -> CallTool:
    """Return an idempotent, reversible Workflow IR execution wrapper."""
    if not enabled() or getattr(call_tool, "_vera_workflow_runtime_wrapped", False):
        return call_tool
    adapter = EmbeddedWorkflowRuntimeAdapter(call_tool)

    async def wrapped(cap_name: str, args: Any, **call_options: Any) -> dict[str, Any]:
        return await adapter.run(cap_name, args, **call_options)

    wrapped._vera_workflow_runtime_wrapped = True  # type: ignore[attr-defined]
    wrapped._vera_native_call_tool = call_tool  # type: ignore[attr-defined]
    return wrapped
