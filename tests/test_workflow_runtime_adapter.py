import pytest

from Vera.vera.execution.workflow_runtime_adapter import (
    EmbeddedWorkflowRuntimeAdapter,
    SCHEMA,
    wrap_call_tool,
)


pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_adapter_executes_exact_action_once_through_native_authority():
    calls = []

    marker = object()

    async def native(cap_name, args, *, session_id="", trace_id="", _stream_cb=None):
        calls.append((cap_name, args, session_id, trace_id, _stream_cb))
        return {"ok": True, "result": {"answer": 42}}

    result = await EmbeddedWorkflowRuntimeAdapter(native).run(
        "math.answer", {"question": "life"},
        session_id="session-1", trace_id="trace-1", _stream_cb=marker,
    )

    assert calls == [("math.answer", {"question": "life"},
                      "session-1", "trace-1", marker)]
    assert result["ok"] is True
    assert result["result"] == {"answer": 42}
    runtime = result["runtime_execution"]
    assert runtime["schema"] == SCHEMA
    assert runtime["execution_authority"] == "native_call_tool"
    assert runtime["workflow_ir"]["authoritative"] is True
    assert runtime["workflow_ir"]["control_mode"] == "native_stepwise"
    assert "question" not in str(runtime)


@pytest.mark.asyncio
async def test_adapter_preserves_native_failure_envelope():
    async def native(*_args, **_kwargs):
        return {"ok": False, "error": "denied"}

    result = await EmbeddedWorkflowRuntimeAdapter(native).run("safe.cap", {})
    assert result["ok"] is False
    assert result["error"] == "denied"
    assert result["runtime_execution"]["runtime_id"] == "vera.embedded-capability"


@pytest.mark.asyncio
async def test_wrapper_is_idempotent_and_can_be_disabled(monkeypatch):
    async def native(*_args, **_kwargs):
        return {"ok": True, "result": None}

    wrapped = wrap_call_tool(native)
    assert wrap_call_tool(wrapped) is wrapped

    monkeypatch.setenv("VERA_AGENT_WORKFLOW_RUNTIME", "0")
    assert wrap_call_tool(native) is native


@pytest.mark.asyncio
async def test_invalid_action_never_reaches_native_caller():
    called = False

    async def native(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    with pytest.raises(ValueError):
        await EmbeddedWorkflowRuntimeAdapter(native).run("", {})
    assert called is False


def test_every_agent_runner_wraps_its_resolved_native_caller():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "dag" /
              "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert source.count(
        "_agent_loop_call_tool = _wrap_workflow_runtime_call(_agent_loop_call_tool)"
    ) == 4
    assert "wrapped = _wrap_workflow_runtime_call(wrapped)" in source
