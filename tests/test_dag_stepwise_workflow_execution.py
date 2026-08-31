import json
from pathlib import Path

import pytest

import Vera.vera.capability_orchestration as orchestration


pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_stepwise_action_exposes_stable_opt_in_identity_without_live_model(monkeypatch):
    decisions = iter([
        {
            "action": "call", "cap": "alpha", "params": {"value": "x"},
            "out_key": "out", "reason": "test",
        },
        {"action": "done", "summary": "complete"},
    ])

    async def generate(*_args, **_kwargs):
        return json.dumps(next(decisions))

    async def alpha(value=""):
        return value.upper()

    monkeypatch.setattr(orchestration, "ollama_generate", generate)
    monkeypatch.setitem(
        orchestration.CAPABILITY_REGISTRY,
        "alpha",
        {
            "func": alpha,
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
    )

    events = [
        event async for event in orchestration._stepwise_run(
            "test", {}, False, 0, include_workflow_ir=True,
        )
    ]
    start = next(data for kind, data in events if kind == "dag.step_start")
    done = next(data for kind, data in events if kind == "dag.step_done")

    assert start["workflow_ir"] == done["workflow_ir"]
    assert start["workflow_ir"]["workflow_hash"].startswith("sha256:")
    assert start["workflow_ir"]["control_mode"] == "native_stepwise"
    assert done["result_preview"] == "X"
    assert events[-1][0] == "dag.complete"
    assert events[-1][1]["state"]["out"] == "X"


@pytest.mark.asyncio
async def test_stepwise_default_events_do_not_gain_provenance_fields(monkeypatch):
    decisions = iter([
        {"action": "call", "cap": "missing", "out_key": "out"},
        {"action": "done", "summary": "complete"},
    ])

    async def generate(*_args, **_kwargs):
        return json.dumps(next(decisions))

    monkeypatch.setattr(orchestration, "ollama_generate", generate)

    events = [
        event async for event in orchestration._stepwise_run(
            "test", {}, False, 0,
        )
    ]

    action_events = [data for kind, data in events if kind in {
        "dag.step_start", "dag.step_error", "dag.step_done",
    }]
    assert action_events
    assert all("workflow_ir" not in data for data in action_events)
    assert events[-1][1]["state"]["out"] == {"error": "unknown capability"}


def test_stream_endpoint_threads_the_opt_in_flag_to_stepwise_execution():
    source = open(orchestration.__file__, encoding="utf-8").read()

    assert "include_workflow_ir=include_workflow_ir" in source


def test_workshop_requests_and_renders_stepwise_definition_evidence():
    panel = (Path(__file__).parents[1] / "vera/dag/dag_workshop_panel.html").read_text(
        encoding="utf-8",
    )

    assert "include_workflow_ir: true" in panel
    assert "function planWorkflowEvidence" in panel
    assert "ev.params||{}" in panel
    assert "t==='dag.hitl_request'||t==='dag.hitl.request'" in panel
