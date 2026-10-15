import sys
from types import SimpleNamespace

import pytest

from vera.execution import workflow_memory_observer as observer


pytestmark = pytest.mark.critical


@pytest.mark.asyncio
async def test_completed_workflow_reaches_memory_hook(monkeypatch):
    calls = []

    async def record_dag_execution(**kwargs):
        calls.append(kwargs)

    hooks = SimpleNamespace(record_dag_execution=record_dag_execution)
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory_hooks", hooks)

    observed = await observer.observe_completed_workflow(
        session_id="session-1",
        dag=[["system.ping", "reply"]],
        state={"host": "localhost"},
        result={"reply": {"ok": True}},
        trigger="stored_dag",
    )

    assert observed is True
    assert calls == [{
        "session_id": "session-1",
        "dag": [["system.ping", "reply"]],
        "state": {"host": "localhost"},
        "result": {"reply": {"ok": True}},
        "agent_name": "",
        "trigger": "stored_dag",
    }]


@pytest.mark.asyncio
async def test_observation_failure_does_not_fail_completed_workflow(monkeypatch):
    async def record_dag_execution(**_kwargs):
        raise RuntimeError("memory unavailable")

    hooks = SimpleNamespace(record_dag_execution=record_dag_execution)
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory_hooks", hooks)

    assert await observer.observe_completed_workflow(
        session_id="session-1", dag=[["system.ping", "reply"]],
        state={}, result={}, trigger="native_dag",
    ) is False


@pytest.mark.asyncio
async def test_empty_workflow_is_not_recorded(monkeypatch):
    monkeypatch.setattr(observer, "_memory_hooks", lambda: (_ for _ in ()).throw(
        AssertionError("empty DAG must not resolve memory hooks")))

    assert await observer.observe_completed_workflow(
        session_id="session-1", dag=[], state={}, result={}, trigger="native_dag",
    ) is False


def test_native_entry_points_use_shared_observer():
    from pathlib import Path

    root = Path(__file__).parents[1]
    orchestration = (root / "vera/capability_orchestration.py").read_text(encoding="utf-8")
    store = (root / "vera/dag/dag_store.py").read_text(encoding="utf-8")

    assert orchestration.count("await observe_completed_workflow(") == 1
    assert store.count("await observe_completed_workflow(") == 2
    assert 'trigger="native_dag"' in orchestration
    assert 'trigger="stored_dag"' in store
    assert 'trigger="monitored_dag"' in store
