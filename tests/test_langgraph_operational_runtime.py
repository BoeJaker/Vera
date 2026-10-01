from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
from types import ModuleType

import pytest

from vera.execution.langgraph_operational_runtime import LangGraphOperationalRunner
from vera.execution.langgraph_workflow_adapter import (
    LangGraphWorkflowRuntimeAdapter,
    compile_langgraph_workflow,
)


pytestmark = pytest.mark.critical


def workflow():
    return {
        "ir_version": "1.0",
        "name": "operational graph",
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
            {"id": "finish", "type": "task", "task": "result.finish",
             "output": "summary"},
        ],
    }


def resign(plan):
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    plan["plan_id"] = "lgplan_" + hashlib.sha256(encoded).hexdigest()
    return plan


class _FakeCompiledGraph:
    def __init__(self, graph):
        self.graph = graph

    async def ainvoke(self, initial):
        state = copy.deepcopy(initial)
        completed = set()
        predecessors = {node: set() for node in self.graph.nodes}
        for source, target in self.graph.edges:
            if target == self.graph.end:
                continue
            if source != self.graph.start:
                predecessors[target].add(source)
        while len(completed) < len(self.graph.nodes):
            ready = [node for node in self.graph.nodes
                     if node not in completed and predecessors[node] <= completed]
            if not ready:
                raise RuntimeError("graph is cyclic")
            results = await asyncio.gather(*(
                self.graph.nodes[node](copy.deepcopy(state)) for node in ready))
            for result in results:
                overlap = set(state["records"]) & set(result["records"])
                if overlap:
                    raise RuntimeError("duplicate node record")
                state["records"].update(result["records"])
            completed.update(ready)
        return state


class _FakeStateGraph:
    def __init__(self, _schema):
        self.nodes = {}
        self.edges = []
        self.start = "__fake_start__"
        self.end = "__fake_end__"

    def add_node(self, name, function):
        self.nodes[name] = function

    def add_edge(self, source, target):
        self.edges.append((source, target))

    def compile(self):
        return _FakeCompiledGraph(self)


@pytest.fixture
def fake_langgraph(monkeypatch):
    package = ModuleType("langgraph")
    graph = ModuleType("langgraph.graph")
    graph.START = "__fake_start__"
    graph.END = "__fake_end__"
    graph.StateGraph = _FakeStateGraph
    monkeypatch.setitem(sys.modules, "langgraph", package)
    monkeypatch.setitem(sys.modules, "langgraph.graph", graph)


@pytest.mark.asyncio
async def test_operational_runner_executes_sequence_parallel_and_guard(fake_langgraph):
    calls = []

    async def execute(task, state):
        calls.append((task, state))
        if task == "data.prepare":
            return {"rows": 2}
        if task == "model.left":
            return "left"
        if task == "model.right":
            return "right"
        return sorted(key for key in state if key.endswith("result"))

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    result = await LangGraphWorkflowRuntimeAdapter(runner).run(
        workflow(), {"run_right": False})

    assert result["status"] == "succeeded"
    assert result["result_state"] == {
        "run_right": False,
        "prepared": {"rows": 2},
        "left_result": "left",
        "summary": ["left_result"],
    }
    assert [task for task, _ in calls] == ["data.prepare", "model.left",
                                            "result.finish"]
    assert calls[-1][1]["prepared"] == {"rows": 2}


@pytest.mark.asyncio
async def test_parallel_outputs_are_visible_once_at_the_join(fake_langgraph):
    observed = []

    async def execute(task, state):
        if task == "result.finish":
            observed.append(copy.deepcopy(state))
            return "done"
        return task

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    result = await LangGraphWorkflowRuntimeAdapter(runner).run(
        workflow(), {"run_right": True})
    assert result["status"] == "succeeded"
    assert observed == [{
        "run_right": True, "prepared": "data.prepare",
        "left_result": "model.left", "right_result": "model.right",
    }]


@pytest.mark.asyncio
async def test_all_tasks_are_authorized_before_any_runtime_or_effect(fake_langgraph):
    calls = 0

    async def execute(*_args):
        nonlocal calls
        calls += 1

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "result.finish"))
    plan = compile_langgraph_workflow(workflow())["plan"]
    with pytest.raises(ValueError, match="unauthorized task"):
        await runner(plan, {"run_right": False})
    assert calls == 0


@pytest.mark.asyncio
async def test_forged_plan_identity_and_authority_fail_before_execution(fake_langgraph):
    calls = 0

    async def execute(*_args):
        nonlocal calls
        calls += 1

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    plan = compile_langgraph_workflow(workflow())["plan"]
    forged = copy.deepcopy(plan)
    forged["nodes"][0]["task"] = "result.finish"
    with pytest.raises(ValueError, match="identity"):
        await runner(forged, {})
    forged = copy.deepcopy(plan)
    forged["execution_authority"] = "langgraph"
    with pytest.raises(ValueError, match="authority"):
        await runner(forged, {})
    assert calls == 0


@pytest.mark.asyncio
async def test_self_identified_malformed_graph_fails_before_execution(fake_langgraph):
    calls = 0

    async def execute(*_args):
        nonlocal calls
        calls += 1

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    plan = copy.deepcopy(compile_langgraph_workflow(workflow())["plan"])
    plan["edges"] = [edge for edge in plan["edges"]
                     if edge != {"from": "prepare", "to": "right"}]
    with pytest.raises(ValueError, match="unreachable"):
        await runner(resign(plan), {})

    duplicated = workflow()
    duplicated["steps"][1]["branches"][1]["output"] = "left_result"
    with pytest.raises(ValueError, match="output keys"):
        await runner(compile_langgraph_workflow(duplicated)["plan"], {})
    assert calls == 0


@pytest.mark.asyncio
async def test_empty_workflow_succeeds_without_loading_runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "langgraph", None)

    async def execute(*_args):
        raise AssertionError("empty workflow must not execute")

    runner = LangGraphOperationalRunner(execute, allowed_tasks=("unused.task",))
    empty = {"ir_version": "1.0", "steps": []}
    result = await LangGraphWorkflowRuntimeAdapter(runner).run(empty, {"seed": 1})
    assert result["status"] == "succeeded"
    assert result["result_state"] == {"seed": 1}


@pytest.mark.asyncio
async def test_missing_runtime_and_executor_failure_are_stable(monkeypatch, fake_langgraph):
    async def execute(_task, _state):
        raise RuntimeError("private backend detail")

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    failed = await LangGraphWorkflowRuntimeAdapter(runner).run(workflow(), {})
    assert failed["status"] == "failed"
    assert failed["error_code"] == "langgraph_execution_failed"

    monkeypatch.setitem(sys.modules, "langgraph", None)
    monkeypatch.delitem(sys.modules, "langgraph.graph", raising=False)
    unavailable = await LangGraphWorkflowRuntimeAdapter(runner).run(workflow(), {})
    assert unavailable["status"] == "failed"
    assert unavailable["error_code"] == "langgraph_runtime_unavailable"


@pytest.mark.asyncio
async def test_cancellation_reaches_the_active_executor(fake_langgraph):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(_task, _state):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = LangGraphOperationalRunner(
        execute, allowed_tasks=("data.prepare", "model.left", "model.right",
                                "result.finish"))
    task = asyncio.create_task(
        LangGraphWorkflowRuntimeAdapter(runner).run(workflow(), {}))
    await started.wait()
    task.cancel()
    result = await task
    assert result["status"] == "cancelled"
    assert cancelled.is_set()


def test_import_does_not_load_langgraph_or_bridge_runtime():
    probe = """
import sys
before = set(sys.modules)
import vera.execution.langgraph_operational_runtime
loaded = set(sys.modules) - before
forbidden = {'langgraph', 'vera.langgraph.langgraph_capabilities', 'docker'}
raise SystemExit(1 if forbidden & loaded else 0)
"""
    completed = subprocess.run([sys.executable, "-c", probe], check=False,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
