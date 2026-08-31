import pytest

import Vera.vera.capability_orchestration as orchestration
import Vera.vera.execution.dag_workflow_execution as workflow_execution


pytestmark = pytest.mark.critical


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _response_text(response):
    chunks = [chunk async for chunk in response.body_iterator]
    return b"".join(
        chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks
    ).decode()


@pytest.mark.asyncio
async def test_hitl_stream_executes_the_prepared_graph(monkeypatch):
    calls = []

    async def prepared_cap():
        calls.append("prepared")
        return "ok"

    monkeypatch.setitem(
        orchestration.CAPABILITY_REGISTRY,
        "prepared.cap",
        {
            "func": prepared_cap,
            "schema": {"type": "object", "properties": {}},
        },
    )

    def prepare(_graph, *, include_workflow_ir=False):
        assert include_workflow_ir is False
        return {
            "graph": [["prepared.cap", "out"]],
            "workflow_ir": None,
        }

    monkeypatch.setattr(workflow_execution, "prepare_streamed_dag_execution", prepare)

    events = [
        event async for event in orchestration._hitl_run_graph_stream(
            [["unprepared.cap", "wrong"]], {}, False, 0,
        )
    ]

    assert calls == ["prepared"]
    assert events[-1][0] == "dag.complete"
    assert events[-1][1]["state"]["out"] == "ok"


@pytest.mark.asyncio
async def test_hitl_stream_reuses_endpoint_preparation_without_reimporting(monkeypatch):
    calls = []

    async def cap():
        calls.append("cap")
        return "ok"

    monkeypatch.setitem(
        orchestration.CAPABILITY_REGISTRY,
        "alpha",
        {"func": cap, "schema": {"type": "object", "properties": {}}},
    )

    def should_not_prepare(*_args, **_kwargs):
        raise AssertionError("endpoint preparation was discarded")

    monkeypatch.setattr(
        workflow_execution, "prepare_streamed_dag_execution", should_not_prepare,
    )
    prepared = {"graph": [["alpha", "out"]], "workflow_ir": None}

    events = [
        event async for event in orchestration._hitl_run_graph_stream(
            [["ignored", "ignored"]], {}, False, 0,
            workflow_prepared=prepared,
        )
    ]

    assert calls == ["cap"]
    assert events[-1][1]["state"]["out"] == "ok"


def test_stream_endpoint_keeps_provenance_opt_in():
    source = open(orchestration.__file__, encoding="utf-8").read()

    assert 'body.get("include_workflow_ir", False)' in source
    assert 'plan_ready["workflow_ir"] = workflow_prepared["workflow_ir"]' in source
    assert "workflow_prepared=workflow_prepared" in source


@pytest.mark.asyncio
async def test_plan_only_stream_keeps_the_legacy_path_without_preparation(monkeypatch):
    async def plan(_goal):
        return {"dag": [["alpha", "out"]], "initial_state": {}, "rationale": ""}

    async def record_stream_activity(**_kwargs):
        return None

    def should_not_prepare(*_args, **_kwargs):
        raise AssertionError("execute=false default should remain inspection-only")

    monkeypatch.setattr(orchestration, "plan_dag", plan)
    monkeypatch.setattr(orchestration, "record_stream_activity", record_stream_activity)
    monkeypatch.setattr(
        workflow_execution, "prepare_streamed_dag_execution", should_not_prepare,
    )

    response = await orchestration.dag_plan_stream_endpoint(
        _Request({"goal": "inspect", "execute": False, "hitl": False}),
    )
    text = await _response_text(response)

    assert '"type": "dag.plan_ready"' in text
    assert '"type": "dag.done"' in text
    assert '"workflow_ir"' not in text


@pytest.mark.asyncio
async def test_executed_stream_prepares_once_and_can_expose_provenance(monkeypatch):
    calls = []
    original = workflow_execution.prepare_streamed_dag_execution

    async def plan(_goal):
        return {"dag": [], "initial_state": {}, "rationale": ""}

    async def record_stream_activity(**_kwargs):
        return None

    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "plan_dag", plan)
    monkeypatch.setattr(orchestration, "record_stream_activity", record_stream_activity)
    monkeypatch.setattr(workflow_execution, "prepare_streamed_dag_execution", prepare)

    response = await orchestration.dag_plan_stream_endpoint(
        _Request({
            "goal": "execute", "execute": True, "hitl": False,
            "include_workflow_ir": True,
        }),
    )
    text = await _response_text(response)

    assert len(calls) == 1
    assert calls[0][1] == {"include_workflow_ir": True}
    assert '"workflow_ir"' in text
    assert '"mode": "workflow_ir_materialized"' in text
    assert '"type": "dag.complete"' in text
