from __future__ import annotations

import subprocess
import sys

import pytest

from vera.execution.temporal_workflow_adapter import (
    PLAN_SCHEMA,
    RESULT_SCHEMA,
    TemporalWorkflowRuntimeAdapter,
    compile_temporal_workflow,
)
from vera.execution.workflow_ir import analyze_adapter, adapter_profiles

pytestmark = pytest.mark.critical


def workflow():
    return {"ir_version": "1.0", "steps": [
        {"id": "collect", "type": "task", "task": "records.collect",
         "output": "records"},
        {"id": "fanout", "type": "parallel", "branches": [
            {"id": "left", "type": "task", "task": "records.left"},
            {"id": "right", "type": "task", "task": "records.right",
             "when": {"kind": "state_truthy", "key": "include_right"}},
        ]},
    ]}


def test_temporal_compiler_uses_shared_content_addressed_plan_contract():
    result = compile_temporal_workflow(workflow())
    assert result["ok"] is True and result["executes"] is False
    plan = result["plan"]
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["adapter"] == "temporal"
    assert plan["plan_id"].startswith("tmplan_")
    assert plan["execution_authority"] == "injected_temporal_runner"
    assert plan["entrypoints"] == ["collect"]
    assert plan["terminal_nodes"] == ["left", "right"]


def test_temporal_profile_is_offline_and_reuses_explicit_gap_analysis():
    profile = adapter_profiles()["profiles"]["temporal"]
    assert profile["available"] is True
    assert profile["executable"] is False
    assert profile["supports"] == ["tasks", "parallel", "conditions"]
    assert analyze_adapter(workflow(), adapter="temporal")["ok"] is True
    unsupported = workflow()
    unsupported["steps"][0]["timeout"] = {"seconds": 5, "owner": "runtime"}
    report = analyze_adapter(unsupported, adapter="temporal")
    assert report["ok"] is False
    assert report["gaps"][0]["path"] == "steps[0].timeout"


@pytest.mark.asyncio
async def test_temporal_injected_runner_must_echo_exact_identity():
    async def runner(plan, state):
        assert state == {"include_right": False}
        return {"runtime_id": "temporal", "plan_id": plan["plan_id"],
                "workflow_hash": plan["workflow_hash"], "status": "succeeded",
                "result_state": {"records": 2}, "extra": "discarded"}

    result = await TemporalWorkflowRuntimeAdapter(runner).run(
        workflow(), {"include_right": False})
    assert result["schema"] == RESULT_SCHEMA
    assert result["result_state"] == {"records": 2}
    assert "extra" not in result


@pytest.mark.asyncio
async def test_temporal_identity_drift_and_unsupported_semantics_fail_before_authority():
    calls = 0

    async def runner(plan, _state):
        nonlocal calls
        calls += 1
        return {"runtime_id": "temporal", "plan_id": "tmplan_forged",
                "workflow_hash": plan["workflow_hash"], "status": "cancelled"}

    adapter = TemporalWorkflowRuntimeAdapter(runner)
    with pytest.raises(ValueError, match="plan_id"):
        await adapter.run(workflow(), {})
    unsupported = workflow()
    unsupported["schedule"] = {"kind": "interval", "seconds": 30}
    blocked = await adapter.run(unsupported, {})
    assert blocked["status"] == "blocked"
    assert calls == 1


def test_import_does_not_load_temporal_langgraph_or_worker_code():
    probe = """
import sys
before = set(sys.modules)
import vera.execution.temporal_workflow_adapter
loaded = set(sys.modules) - before
forbidden = {'temporalio', 'langgraph', 'docker'}
raise SystemExit(1 if forbidden & loaded else 0)
"""
    completed = subprocess.run([sys.executable, "-c", probe], check=False,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
