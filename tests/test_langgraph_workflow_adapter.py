from __future__ import annotations

import subprocess
import sys

import pytest

from vera.execution.langgraph_workflow_adapter import (
    LangGraphWorkflowRuntimeAdapter,
    PLAN_SCHEMA,
    RESULT_SCHEMA,
    compile_langgraph_workflow,
)
from vera.execution.workflow_ir import analyze_adapter, adapter_profiles


pytestmark = pytest.mark.critical


def workflow():
    return {
        "ir_version": "1.0",
        "name": "prepare and compare",
        "steps": [
            {"id": "prepare", "type": "task", "task": "data.prepare",
             "output": "prepared"},
            {"id": "compare", "type": "parallel", "branches": [
                {"id": "left", "type": "task", "task": "model.left",
                 "output": "left_result"},
                {"id": "right", "type": "task", "task": "model.right",
                 "output": "right_result", "when": {
                     "kind": "state_truthy", "key": "run_right"}},
            ]},
            {"id": "summarize", "type": "task", "task": "result.summarize",
             "output": "summary"},
        ],
    }


def test_compiler_is_content_addressed_and_preserves_sequence_parallel_and_guard():
    first = compile_langgraph_workflow(workflow())
    second = compile_langgraph_workflow(workflow())
    assert first == second
    assert first["ok"] is True and first["executes"] is False
    plan = first["plan"]
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["plan_id"].startswith("lgplan_")
    assert plan["entrypoints"] == ["prepare"]
    assert plan["terminal_nodes"] == ["summarize"]
    assert plan["edges"] == [
        {"from": "__start__", "to": "prepare"},
        {"from": "prepare", "to": "left"},
        {"from": "prepare", "to": "right"},
        {"from": "left", "to": "summarize"},
        {"from": "right", "to": "summarize"},
    ]
    right = next(node for node in plan["nodes"] if node["id"] == "right")
    assert right["guard"] == {"kind": "state_truthy", "key": "run_right"}


def test_invalid_and_semantically_unsupported_workflows_fail_before_a_plan_exists():
    invalid = compile_langgraph_workflow({"ir_version": "9", "steps": []})
    assert invalid["gaps"][0]["code"] == "invalid_workflow"
    assert "plan" not in invalid

    richer = workflow()
    richer["steps"][0]["retry"] = {"max_attempts": 2, "owner": "runtime"}
    blocked = compile_langgraph_workflow(richer)
    assert blocked["ok"] is False
    assert blocked["gaps"][0]["path"] == "steps[0].retry"
    assert "plan" not in blocked
    assert compile_langgraph_workflow([])["gaps"][0]["code"] == "invalid_workflow"


def test_profile_and_analysis_claim_only_the_offline_proven_subset():
    profile = adapter_profiles()["profiles"]["langgraph"]
    assert profile == {
        "available": True,
        "executable": False,
        "supports": ["tasks", "parallel", "conditions"],
        "detail": "Offline compiler plus an opt-in operational runner; no policy executor is bound or default route registered.",
    }
    analysis = analyze_adapter(workflow(), adapter="langgraph")
    assert analysis["ok"] is True
    assert analysis["executes"] is False
    assert analysis["gaps"] == []


@pytest.mark.asyncio
async def test_injected_runtime_receives_exact_plan_once_and_returns_bound_result():
    calls = []

    async def runner(plan, input_state):
        calls.append((plan, input_state))
        return {"runtime_id": "langgraph", "plan_id": plan["plan_id"],
                "workflow_hash": plan["workflow_hash"], "status": "succeeded",
                "result_state": {"summary": "ok"}, "untrusted_extra": "dropped"}

    result = await LangGraphWorkflowRuntimeAdapter(runner).run(
        workflow(), {"run_right": True})
    assert len(calls) == 1
    assert calls[0][1] == {"run_right": True}
    assert result["schema"] == RESULT_SCHEMA
    assert result["result_state"] == {"summary": "ok"}
    assert result["runtime_execution"]["plan_id"] == calls[0][0]["plan_id"]
    assert "untrusted_extra" not in result


@pytest.mark.asyncio
async def test_blocked_workflow_never_reaches_runner():
    called = False

    async def runner(*_args):
        nonlocal called
        called = True

    unsupported = workflow()
    unsupported["schedule"] = {"kind": "interval", "seconds": 60}
    result = await LangGraphWorkflowRuntimeAdapter(runner).run(unsupported, {})
    assert result["status"] == "blocked"
    assert result["gaps"][0]["path"] == "schedule"
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("plan_id", "lgplan_forged"),
    ("workflow_hash", "sha256:forged"),
    ("runtime_id", "another-runtime"),
])
async def test_runtime_rejects_identity_drift(field, value):
    async def runner(plan, _state):
        response = {"runtime_id": "langgraph", "plan_id": plan["plan_id"],
                    "workflow_hash": plan["workflow_hash"], "status": "cancelled"}
        response[field] = value
        return response

    with pytest.raises(ValueError, match=field):
        await LangGraphWorkflowRuntimeAdapter(runner).run(workflow(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("response,match", [
    ({"status": "running"}, "terminal status"),
    ({"status": "succeeded"}, "result_state"),
    ({"status": "failed"}, "error_code"),
    ({"status": "cancelled", "result_state": {}}, "cannot contain result_state"),
])
async def test_runtime_rejects_ambiguous_terminal_envelopes(response, match):
    async def runner(plan, _state):
        return {"runtime_id": "langgraph", "plan_id": plan["plan_id"],
                "workflow_hash": plan["workflow_hash"], **response}

    with pytest.raises(ValueError, match=match):
        await LangGraphWorkflowRuntimeAdapter(runner).run(workflow(), {})


@pytest.mark.asyncio
async def test_input_state_must_be_bounded_canonical_json():
    async def runner(*_args):
        raise AssertionError("runner must not be called")

    adapter = LangGraphWorkflowRuntimeAdapter(runner)
    with pytest.raises(ValueError, match="canonical JSON"):
        await adapter.run(workflow(), {"bad": object()})
    with pytest.raises(ValueError, match="one MiB"):
        await adapter.run(workflow(), {"large": "x" * 1_048_576})


@pytest.mark.asyncio
async def test_success_result_state_must_be_bounded_canonical_json():
    response_state = {"bad": object()}

    async def runner(plan, _state):
        return {"runtime_id": "langgraph", "plan_id": plan["plan_id"],
                "workflow_hash": plan["workflow_hash"], "status": "succeeded",
                "result_state": response_state}

    adapter = LangGraphWorkflowRuntimeAdapter(runner)
    with pytest.raises(ValueError, match="canonical JSON"):
        await adapter.run(workflow(), {})
    response_state = {"large": "x" * 1_048_576}
    with pytest.raises(ValueError, match="one MiB"):
        await adapter.run(workflow(), {})


def test_import_does_not_load_langgraph_or_bridge_runtime():
    probe = """
import sys
before = set(sys.modules)
import vera.execution.langgraph_workflow_adapter
loaded = set(sys.modules) - before
forbidden = {'langgraph', 'vera.langgraph.langgraph_capabilities', 'docker'}
raise SystemExit(1 if forbidden & loaded else 0)
"""
    completed = subprocess.run([sys.executable, "-c", probe], check=False,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
