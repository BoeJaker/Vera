import pytest

import Vera.vera.capability_orchestration as orchestration
import Vera.vera.dag.dag_store as dag_store


pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_unsupervised_monitor_executes_the_ir_materialized_graph(monkeypatch):
    source = [["alpha", "out"]]
    observed = {}
    events = []

    async def run_graph(graph, state):
        observed["graph"] = graph
        observed["state"] = state
        return {**state, "out": "ok"}

    async def should_not_supervise(*_args, **_kwargs):
        raise AssertionError("unsupervised execution reached supervised runner")

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(orchestration, "run_graph", run_graph)
    monkeypatch.setattr(orchestration, "supervised_run_graph", should_not_supervise)
    monkeypatch.setattr(dag_store, "emit_event", emit)

    result = await dag_store.ExecutionMonitor().run_and_correct(
        source, {"input": 1}, "trace-1", max_corrections=0,
        include_workflow_ir=True,
    )

    assert observed["graph"] == source
    assert observed["graph"] is not source
    assert observed["state"] == {"input": 1}
    assert result["result"] == {"input": 1, "out": "ok"}
    assert result["workflow_ir"]["mode"] == "workflow_ir_materialized"
    assert result["workflow_ir"]["workflow_hash"].startswith("sha256:")
    assert "graph" not in result["workflow_ir"]
    assert events[-1]["workflow_id"] == result["workflow_ir"]["workflow_hash"]


@pytest.mark.asyncio
async def test_supervised_monitor_preserves_the_original_native_graph(monkeypatch):
    source = [["alpha", "out"]]
    observed = {}

    async def should_not_run_plain(*_args, **_kwargs):
        raise AssertionError("supervised execution reached plain runner")

    async def supervised_run_graph(graph, state):
        observed["graph"] = graph
        return state

    async def emit(_event):
        return None

    monkeypatch.setattr(orchestration, "run_graph", should_not_run_plain)
    monkeypatch.setattr(orchestration, "supervised_run_graph", supervised_run_graph)
    monkeypatch.setattr(dag_store, "emit_event", emit)

    result = await dag_store.ExecutionMonitor().run_and_correct(
        source, {}, "trace-2", max_corrections=0, supervised=True,
        include_workflow_ir=True,
    )

    assert observed["graph"] is source
    assert result["workflow_ir"]["mode"] == "native_supervised"
    assert result["workflow_ir"]["authoritative"] is False


@pytest.mark.asyncio
async def test_monitor_default_response_shape_and_callable_compatibility_are_preserved(monkeypatch):
    def condition(_state):
        return True

    source = [["alpha", "out", condition]]
    observed = {}

    async def run_graph(graph, state):
        observed["graph"] = graph
        return state

    async def emit(_event):
        return None

    monkeypatch.setattr(orchestration, "run_graph", run_graph)
    monkeypatch.setattr(dag_store, "emit_event", emit)

    result = await dag_store.ExecutionMonitor().run_and_correct(
        source, {}, "trace-3", max_corrections=0,
    )

    assert observed["graph"] is source
    assert set(result) == {
        "result", "runtime_ms", "errors_found", "corrections",
        "execution_report",
    }
