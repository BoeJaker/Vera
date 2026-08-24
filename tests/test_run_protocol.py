import asyncio

import pytest

from vera.execution.run_protocol import (
    ArtifactRef,
    PROTOCOL_VERSION,
    Run,
    RunControl,
    RunEvent,
    RunStatus,
    replay_run,
)
from vera.execution.run_shadow import execute_dag_with_run_shadow


pytestmark = pytest.mark.critical


def test_run_lifecycle_has_monotonic_events_and_terminal_timestamps():
    run = Run(id="run-1", kind="vera.dag", trace_id="trace-1")
    started = run.transition(RunStatus.RUNNING, occurred_at="2026-01-01T00:00:00Z")
    completed = run.transition(RunStatus.COMPLETED,
                               occurred_at="2026-01-01T00:00:01Z")

    assert [started.sequence, completed.sequence] == [1, 2]
    assert run.started_at == "2026-01-01T00:00:00Z"
    assert run.ended_at == "2026-01-01T00:00:01Z"
    assert run.to_dict()["protocol"] == PROTOCOL_VERSION
    assert run.to_dict()["status"] == "completed"


@pytest.mark.parametrize("source,target", [
    (RunStatus.CREATED, RunStatus.COMPLETED),
    (RunStatus.RUNNING, RunStatus.CREATED),
    (RunStatus.COMPLETED, RunStatus.RUNNING),
])
def test_invalid_transitions_are_refused(source, target):
    run = Run(id="run-1", kind="vera.dag", status=source)
    with pytest.raises(ValueError, match="invalid run transition"):
        run.transition(target)


def test_retry_wait_approval_cancel_and_timeout_transitions_are_explicit():
    retry = Run(id="retry", kind="vera.dag", status=RunStatus.RUNNING)
    retry.transition(RunStatus.RETRYING)
    retry.transition(RunStatus.RUNNING)
    retry.transition(RunStatus.FAILED)

    approval = Run(id="approval", kind="vera.dag", status=RunStatus.RUNNING)
    approval.transition(RunStatus.APPROVAL_PENDING)
    approval.transition(RunStatus.CANCELLED)

    waiting = Run(id="waiting", kind="vera.dag", status=RunStatus.RUNNING)
    waiting.transition(RunStatus.WAITING)
    waiting.transition(RunStatus.TIMED_OUT)

    assert retry.status == RunStatus.FAILED
    assert approval.status == RunStatus.CANCELLED
    assert waiting.status == RunStatus.TIMED_OUT


def test_replay_rebuilds_projection_and_rejects_gaps_or_foreign_events():
    original = Run(id="run-1", kind="vera.dag")
    original.transition(RunStatus.RUNNING, occurred_at="t1")
    original.transition(RunStatus.COMPLETED, occurred_at="t2")
    replayed = replay_run(Run(id="run-1", kind="vera.dag"), original.events)
    assert replayed.status == RunStatus.COMPLETED
    assert [event.sequence for event in replayed.events] == [1, 2]
    assert [event.id for event in replayed.events] == [event.id for event in original.events]

    gap = RunEvent(id="e", run_id="run-1", sequence=2, type="run.started",
                   status=RunStatus.RUNNING, occurred_at="t1")
    with pytest.raises(ValueError, match="monotonic"):
        replay_run(Run(id="run-1", kind="vera.dag"), [gap])

    foreign = RunEvent(id="e", run_id="other", sequence=1, type="run.started",
                       status=RunStatus.RUNNING, occurred_at="t1")
    with pytest.raises(ValueError, match="another run"):
        replay_run(Run(id="run-1", kind="vera.dag"), [foreign])


def test_artifact_and_control_contracts_are_runtime_neutral():
    artifact = ArtifactRef(id="a1", kind="report", uri="fabric://reports/1",
                           checksum="sha256:abc")
    control = RunControl(id="c1", run_id="r1", action="cancel",
                         requested_at="t1", requested_by="user")
    assert artifact.uri.startswith("fabric://")
    assert control.status == "requested"


def test_portable_observations_round_trip_through_events_and_replay():
    run = Run(id="observed", kind="external.workflow")
    run.transition(RunStatus.RUNNING, payload={
        "progress": 0.25,
        "usage": {"input_tokens": 12},
        "cost": {"amount": "0.01", "currency": "USD"},
        "policy": {"decision": "allowed", "policy_id": "safe-tools-v2"},
        "attempt": 2,
        "retry_owner": "native_engine",
    })
    run.record_event("run.progress", payload={
        "progress": 0.75,
        "usage": {"output_tokens": 4},
    })

    value = run.to_dict()
    assert value["progress"] == 0.75
    assert value["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert value["cost"] == {"amount": "0.01", "currency": "USD"}
    assert value["policy"]["decision"] == "allowed"
    assert value["attempt"] == 2
    assert value["retry_owner"] == "native_engine"

    replayed = replay_run(Run(id="observed", kind="external.workflow",
                              created_at=run.created_at), run.events)
    assert replayed.to_dict(include_events=False) == run.to_dict(include_events=False)


@pytest.mark.parametrize("progress", [-0.01, 1.01, float("nan"), float("inf")])
def test_progress_observations_reject_out_of_range_or_non_finite_values(progress):
    run = Run(id="observed", kind="test")
    with pytest.raises(ValueError, match="finite number from 0 to 1"):
        run.transition(RunStatus.RUNNING, payload={"progress": progress})
    assert run.status == RunStatus.CREATED
    assert run.events == []


def test_usage_cost_and_policy_observations_require_mappings():
    run = Run(id="observed", kind="test", status=RunStatus.RUNNING)
    for field in ("usage", "cost", "policy"):
        with pytest.raises(ValueError, match=f"{field} observation must be a mapping"):
            run.record_event("run.observed", payload={field: "opaque"})
    assert run.events == []


def test_shadow_adapter_preserves_native_result_and_emits_standard_events():
    events = []
    native_result = {"answer": 42}
    state = {"input": "unchanged"}

    async def executor(graph, received_state, trace_id, observer):
        assert graph == [["example.cap", "answer"]]
        assert received_state is state
        assert trace_id == "trace-1"
        assert observer is not None
        return native_result

    async def emit(event):
        events.append(event)

    result = asyncio.run(execute_dag_with_run_shadow(
        executor=executor, graph=[["example.cap", "answer"]], state=state,
        trace_id="trace-1", emit=emit))

    assert result is native_result
    assert [item["event"]["type"] for item in events] == [
        "run.started", "run.completed"]
    assert all(item["protocol"] == PROTOCOL_VERSION for item in events)
    assert events[0]["run"]["trace_id"] == "trace-1"


def test_shadow_emit_failure_does_not_change_dag_result():
    async def executor(graph, state, trace_id, observer):
        return {"same": True}

    async def broken_emit(event):
        raise RuntimeError("observability unavailable")

    result = asyncio.run(execute_dag_with_run_shadow(
        executor=executor, graph=[], state={}, trace_id="trace", emit=broken_emit))
    assert result == {"same": True}


@pytest.mark.parametrize("error,terminal", [
    (RuntimeError("native failure"), "failed"),
    (TimeoutError("late"), "timed_out"),
])
def test_shadow_adapter_reraises_native_errors_after_terminal_event(error, terminal):
    events = []

    async def executor(graph, state, trace_id, observer):
        raise error

    async def emit(event):
        events.append(event)

    with pytest.raises(type(error), match=str(error)):
        asyncio.run(execute_dag_with_run_shadow(
            executor=executor, graph=[], state={}, trace_id="trace", emit=emit))
    assert events[-1]["event"]["status"] == terminal


def test_shadow_adapter_preserves_native_cancellation():
    events = []

    async def executor(graph, state, trace_id, observer):
        raise asyncio.CancelledError()

    async def emit(event):
        events.append(event)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_dag_with_run_shadow(
            executor=executor, graph=[], state={}, trace_id="trace", emit=emit))
    assert events[-1]["event"]["status"] == "cancelled"


def test_dag_run_capability_keeps_existing_response_shape(monkeypatch):
    from vera import capability_orchestration as orchestration

    events = []
    native_result = {"input": "same", "answer": 42}

    async def native(graph, state, trace_id="", run_observer=None):
        assert graph == [["test.cap", "answer"]]
        assert trace_id == "trace-1"
        assert run_observer is not None
        return native_result

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(orchestration, "run_graph", native)
    monkeypatch.setattr(orchestration, "emit_event", emit)

    result = asyncio.run(orchestration.cap_dag_run.__wrapped__(
        dag=[["test.cap", "answer"]], state={"input": "same"},
        supervised=False, trace_id="trace-1"))

    assert result == {"trace_id": "trace-1", "result": native_result}
    assert set(result) == {"trace_id", "result"}
    assert [event["event"]["type"] for event in events] == [
        "run.started", "run.completed"]
