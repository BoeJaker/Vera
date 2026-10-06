import pytest

from vera.execution.workflow_ir import import_native_dag


pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_native_ir_capability_delegates_exact_definition_once(monkeypatch):
    from vera import capability_orchestration as orchestration

    workflow = import_native_dag([["alpha", "out"]], name="portable")["workflow"]
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs)
        return {
            "trace_id": "trace-1",
            "result": {"out": {"ok": True}},
            "workflow_ir": {"workflow_hash": kwargs["workflow_evidence"]["workflow_hash"]},
        }

    monkeypatch.setattr(
        orchestration, "_execute_native_dag_with_workflow_evidence", execute,
    )

    result = await orchestration.cap_workflow_ir_run_native(
        workflow=workflow, state={"input": 1}, session_id="session-1",
        trace_id="trace-1",
    )

    assert result["ok"] is True
    assert result["executes"] is True
    assert len(calls) == 1
    assert calls[0]["workflow_evidence"]["workflow_hash"] == workflow["content_hash"]
    assert calls[0]["workflow_evidence"]["graph"] == [["alpha", "out"]]
    assert calls[0]["state"] == {"input": 1}
    assert calls[0]["supervised"] is False
    assert calls[0]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_native_ir_capability_never_dispatches_unsupported_semantics(monkeypatch):
    from vera import capability_orchestration as orchestration

    workflow = import_native_dag([["alpha", "out"]], name="portable")["workflow"]
    workflow["steps"][0]["timeout"] = {"seconds": 5, "owner": "runtime"}

    async def must_not_execute(**_kwargs):
        raise AssertionError("unsupported Workflow IR reached the native executor")

    monkeypatch.setattr(
        orchestration, "_execute_native_dag_with_workflow_evidence", must_not_execute,
    )

    result = await orchestration.cap_workflow_ir_run_native(workflow=workflow)

    assert result["ok"] is False
    assert result["error"] == "unsupported_workflow_semantics"
    assert result["executes"] is False
    assert result["workflow_ir"]["gaps"][0]["blocking"] is True


@pytest.mark.asyncio
async def test_native_ir_capability_rejects_non_object_state_before_dispatch(monkeypatch):
    from vera import capability_orchestration as orchestration

    workflow = import_native_dag([["alpha", "out"]], name="portable")["workflow"]

    async def must_not_execute(**_kwargs):
        raise AssertionError("invalid state reached the native executor")

    monkeypatch.setattr(
        orchestration, "_execute_native_dag_with_workflow_evidence", must_not_execute,
    )

    result = await orchestration.cap_workflow_ir_run_native(
        workflow=workflow, state=["not", "an", "object"],
    )

    assert result == {
        "ok": False,
        "error": "invalid_state",
        "detail": "state must be an object",
        "executes": False,
    }
