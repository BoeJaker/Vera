import asyncio
import sqlite3
import time

import pytest

from vera.execution.run_journal import SqliteRunJournal
from vera.execution.run_projection import DagRunObserver, ShadowRunRegistry
from vera.execution.run_protocol import Run, RunStatus


pytestmark = pytest.mark.critical


async def _ignore(_event):
    return None


def test_observer_projects_child_lineage_progress_artifact_and_journal():
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag", trace_id="trace", workflow_id="trace",
                 session_id="chat-1")
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
    assert child["session_id"] == "chat-1"
    assert child["task_id"] == "0"
    assert child["attempt"] == 1
    assert child["status"] == "completed"
    assert child["events"][0]["causation_id"] == parent.events[0].id
    assert child["events"][1]["causation_id"] == child["events"][0]["id"]
    assert parent.events[-1].causation_id == child["events"][1]["id"]
    assert child["artifacts"][0]["uri"].endswith("/result")
    assert child["artifacts"][0]["checksum"].startswith("sha256:")


def test_registry_graph_is_session_scoped_content_free_and_non_authoritative():
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag", trace_id="trace",
                 session_id="chat-1")
    registry.record(parent, parent.transition(RunStatus.RUNNING,
                                              event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["example.cap", "answer"]],
                              emit=_ignore, registry=registry)
    asyncio.run(observer.node_started((0,), "example.cap"))
    other = Run(id="other", kind="vera.dag", session_id="chat-2")
    registry.record(other, other.transition(RunStatus.RUNNING,
                                            event_type="run.started"))

    graph = registry.graph(session_id="chat-1")

    assert graph["authoritative"] is False
    assert {node["id"] for node in graph["nodes"]} == {"run:parent", *[
        "run:" + child["id"] for child in registry.get("parent")["children"]]}
    assert graph["edges"] == [{
        "from": "run:parent", "to": graph["nodes"][1]["id"],
        "from_id": "run:parent", "to_id": graph["nodes"][1]["id"],
        "type": "RUN_CHILD", "relation": "RUN_CHILD", "source": "run",
    }]
    assert all(node["non_authoritative"] for node in graph["nodes"])
    assert all("artifacts" not in node and "events" not in node
               for node in graph["nodes"])
    assert "run:other" not in {node["id"] for node in graph["nodes"]}


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
    assert children["1"]["artifacts"][0]["kind"] == "capability.partial_output"
    assert parent.progress == 1.0


def test_supervised_dag_projects_retry_attempt_without_changing_result(monkeypatch):
    from vera import capability_orchestration as orchestration

    calls = 0
    async def flaky(trace_id=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    monkeypatch.setitem(orchestration.CAPABILITY_REGISTRY, "test.flaky", {
        "schema": {"properties": {}}, "func": flaky})
    registry = ShadowRunRegistry()
    parent = Run(id="parent", kind="vera.dag", trace_id="trace")
    registry.record(parent, parent.transition(RunStatus.RUNNING,
                                              event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["test.flaky", "answer"]],
                              emit=_ignore, registry=registry)

    result = asyncio.run(orchestration.supervised_run_graph(
        [["test.flaky", "answer"]], {}, trace_id="trace", run_observer=observer))

    child = registry.get("parent")["children"][0]
    assert result == {"answer": {"ok": True}}
    assert child["status"] == "completed"
    assert child["attempt"] == 2
    assert child["error"] is None
    assert [event["type"] for event in child["events"]] == [
        "run.started", "run.retrying", "run.retry.started", "run.completed"]


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


def test_projection_eviction_never_deletes_durable_journal(tmp_path):
    journal = SqliteRunJournal(tmp_path / "runs.sqlite3")
    registry = ShadowRunRegistry(max_runs=1, journal=journal)
    first = Run(id="first", kind="test")
    second = Run(id="second", kind="test")
    registry.record(first, first.transition(RunStatus.RUNNING))
    registry.record(second, second.transition(RunStatus.RUNNING))

    assert registry.get("first") is None
    assert journal.export("first")["event_count"] == 1
    journal.close()


def test_sqlite_registry_recovers_searchable_parent_child_catalog(tmp_path):
    path = tmp_path / "runs.sqlite3"
    journal = SqliteRunJournal(path)
    registry = ShadowRunRegistry(max_runs=20, journal=journal)
    parent = Run(id="parent", kind="vera.dag", trace_id="trace",
                 workflow_id="workflow", session_id="chat-1")
    registry.record(parent, parent.transition(RunStatus.RUNNING,
                                              event_type="run.started"))
    observer = DagRunObserver(parent=parent, graph=[["example.cap", "answer"]],
                              emit=_ignore, registry=registry)
    asyncio.run(observer.node_started((0,), "example.cap"))
    asyncio.run(observer.node_finished((0,), "example.cap", {"answer": 42}))
    journal.close()

    reopened = SqliteRunJournal(path)
    recovered = ShadowRunRegistry(max_runs=20, journal=reopened)
    projection = recovered.get("parent")

    assert recovered.recovery == {
        "attempted": True, "recovered": 2, "failed": 0, "failures": []}
    assert projection["run"]["progress"] == 1.0
    assert projection["run"]["workflow_id"] == "workflow"
    assert projection["run"]["session_id"] == "chat-1"
    assert projection["children"][0]["status"] == "completed"
    assert projection["children"][0]["artifacts"][0]["checksum"].startswith("sha256:")
    assert recovered.graph(session_id="chat-1")["count"] == 2
    reopened.close()


def test_sqlite_registry_isolates_corrupt_run_during_recovery(tmp_path):
    path = tmp_path / "runs.sqlite3"
    journal = SqliteRunJournal(path)
    registry = ShadowRunRegistry(max_runs=20, journal=journal)
    for run_id in ("healthy", "broken"):
        run = Run(id=run_id, kind="test", session_id="session")
        registry.record(run, run.transition(RunStatus.RUNNING,
                                            event_type="run.started"))
    journal.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE run_events SET checksum='damaged' WHERE run_id='broken'")
        db.commit()

    reopened = SqliteRunJournal(path)
    recovered = ShadowRunRegistry(max_runs=20, journal=reopened)

    assert recovered.get("healthy") is not None
    assert recovered.get("broken") is None
    assert recovered.recovery["recovered"] == 1
    assert recovered.recovery["failed"] == 1
    assert recovered.recovery["failures"] == [{
        "run_id": "broken", "error_type": "JournalCorruption"}]
    public = recovered.recovery_status()
    assert public["quarantined"] == 1
    assert public["read_only"] is True
    assert public["quarantined_refs"][0]["error_type"] == "JournalCorruption"
    assert len(public["quarantined_refs"][0]["run_ref"]) == 12
    assert "broken" not in str(public)
    assert recovered.graph()["recovery"] == public
    reopened.close()


def test_sqlite_registry_recovery_keeps_newest_bounded_catalog(tmp_path):
    path = tmp_path / "runs.sqlite3"
    journal = SqliteRunJournal(path)
    registry = ShadowRunRegistry(max_runs=20, journal=journal)
    for run_id in ("first", "second", "third"):
        run = Run(id=run_id, kind="test")
        registry.record(run, run.transition(RunStatus.RUNNING))
    journal.close()

    reopened = SqliteRunJournal(path)
    recovered = ShadowRunRegistry(max_runs=2, journal=reopened)

    assert [item["id"] for item in recovered.list()] == ["third", "second"]
    assert recovered.get("first") is None
    reopened.close()


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
