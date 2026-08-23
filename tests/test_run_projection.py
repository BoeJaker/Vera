import asyncio
import time

import pytest

from vera.execution.run_projection import DagRunObserver, ShadowRunRegistry
from vera.execution.run_protocol import Run, RunStatus


pytestmark = pytest.mark.critical


async def _ignore(_event):
    return None


def test_observer_projects_child_lineage_progress_artifact_and_journal():
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag", trace_id="trace", workflow_id="trace")
    registry.record(parent, parent.transition(RunStatus.RUNNING, event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["example.cap", "answer"]],
                              emit=_ignore, registry=registry)

    asyncio.run(observer.node_started((0,), "example.cap"))
    asyncio.run(observer.node_finished((0,), "example.cap", {"answer": 42}))

    projection = registry.get("parent")
    child = projection["children"][0]
    assert projection["authoritative"] is False
    assert projection["journal"]["ok"] is True
    assert parent.progress == 1.0
    assert child["parent_run_id"] == "parent"
    assert child["workflow_id"] == "trace"
    assert child["task_id"] == "0"
    assert child["attempt"] == 1
    assert child["status"] == "completed"
    assert child["events"][0]["causation_id"] == parent.events[0].id
    assert child["events"][1]["causation_id"] == child["events"][0]["id"]
    assert parent.events[-1].causation_id == child["events"][1]["id"]
    assert child["artifacts"][0]["uri"].endswith("/result")
    assert child["artifacts"][0]["checksum"].startswith("sha256:")


def test_observer_projects_skipped_and_failed_children():
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag")
    observer = DagRunObserver(parent=parent,
                              graph=[["skip.cap", "a"], ["fail.cap", "b"]],
                              emit=_ignore, registry=registry)
    asyncio.run(observer.node_skipped((0,), "skip.cap", "condition_false"))
    asyncio.run(observer.node_started((1,), "fail.cap"))
    asyncio.run(observer.node_finished((1,), "fail.cap", {"error": "boom"}, "boom"))

    children = {run["task_id"]: run for run in registry.get("parent")["children"]}
    assert children["0"]["status"] == "skipped"
    assert children["1"]["status"] == "failed"
    assert children["1"]["error"]["code"] == "dag_node_error"
    assert parent.progress == 1.0


def test_registry_is_bounded_and_exports_checksummed_events():
    registry = ShadowRunRegistry(max_runs=1)
    first = Run(id="first", kind="test")
    second = Run(id="second", kind="test")
    registry.record(first, first.transition(RunStatus.RUNNING))
    registry.record(second, second.transition(RunStatus.RUNNING))
    assert registry.get("first") is None
    assert [item["id"] for item in registry.list()] == ["second"]
    exported = registry.journal.export("second")
    assert exported["event_count"] == 1
    assert exported["last_checksum"]


def test_run_graph_observes_sequential_parallel_conditional_and_error(monkeypatch):
    from vera import capability_orchestration as orchestration

    async def ok(value=None, trace_id=None):
        return {"value": value, "trace": trace_id}

    async def fail(trace_id=None):
        return {"error": "expected"}

    monkeypatch.setitem(orchestration.CAPABILITY_REGISTRY, "test.ok", {
        "schema": {"properties": {"value": {"type": "string"}}}, "func": ok})
    monkeypatch.setitem(orchestration.CAPABILITY_REGISTRY, "test.fail", {
        "schema": {"properties": {}}, "func": fail})
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag", trace_id="trace", workflow_id="trace")
    observer = DagRunObserver(parent=parent, graph=[
        ["test.ok", "one"],
        [["test.ok", "two"], ["test.fail", "bad"]],
        ["test.ok", "skipped", "CONDITION:no"],
    ], emit=_ignore, registry=registry)

    result = asyncio.run(orchestration.run_graph([
        ["test.ok", "one"],
        [["test.ok", "two"], ["test.fail", "bad"]],
        ["test.ok", "skipped", "CONDITION:no"],
    ], {"value": "same"}, "trace", observer))

    statuses = sorted(run["status"] for run in registry.get("parent")["children"])
    assert statuses == ["completed", "completed", "failed", "skipped"]
    assert result["bad"] == {"error": "expected"}
    assert "skipped" not in result
    assert parent.progress == 1.0


def test_observer_overhead_stays_below_generous_shadow_budget():
    registry = ShadowRunRegistry(max_runs=1000)
    parent = Run(id="parent", kind="vera.dag")
    graph = [["test.cap", f"out_{index}"] for index in range(100)]
    observer = DagRunObserver(parent=parent, graph=graph, emit=_ignore, registry=registry)

    started = time.perf_counter()
    async def exercise():
        for index in range(100):
            await observer.node_started((index,), "test.cap")
            await observer.node_finished((index,), "test.cap", {"ok": True})
    asyncio.run(exercise())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert len(registry.list(limit=200)) == 101
