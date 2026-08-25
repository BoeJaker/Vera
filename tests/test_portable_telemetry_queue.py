import asyncio
import json

import pytest

from vera.execution.portable_telemetry import (
    PortableTelemetryQueue,
    project_run_trace,
    queue_config,
)
from vera.execution.run_protocol import Run, RunStatus
from vera.execution.run_projection import ShadowRunRegistry


pytestmark = pytest.mark.critical


def _env(**values):
    return {
        "VERA_OTLP_AUTO_EXPORT": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "VERA_OTLP_FLUSH_MS": "0",
        **values,
    }


def _completed(run_id):
    run = Run(id=run_id, kind="vera.dag")
    run.transition(RunStatus.RUNNING, occurred_at="2026-01-01T00:00:00Z")
    run.transition(RunStatus.COMPLETED, occurred_at="2026-01-01T00:00:01Z")
    return run


def test_queue_requires_both_explicit_auto_export_and_valid_exporter():
    assert queue_config({}).reason == "auto_export_disabled"
    assert queue_config({"VERA_OTLP_AUTO_EXPORT": "1"}).reason == "not_configured"
    config = queue_config(_env(VERA_OTLP_QUEUE_SIZE="9999",
                               VERA_OTLP_BATCH_SIZE="9999"))
    assert config.enabled is True
    assert config.max_queue == 256 and config.batch_size == 32


def test_disabled_queue_has_no_worker_and_never_calls_sender():
    called = False

    async def sender(*args):
        nonlocal called
        called = True
        return 200

    queue = PortableTelemetryQueue(sender=sender, environ={})
    assert queue.offer_run(_completed("disabled")) is False
    assert queue.status()["worker_running"] is False
    assert queue.status()["offered"] == 0
    assert called is False


def test_queue_batches_traces_and_flushes_on_close():
    captured = []

    async def sender(endpoint, body, headers, timeout):
        captured.append(json.loads(body))
        return 200

    async def exercise():
        queue = PortableTelemetryQueue(sender=sender, environ=_env())
        assert queue.offer_run(_completed("one")) is True
        assert queue.offer_run(_completed("two")) is True
        await queue._queue.join()
        status = queue.status()
        await queue.close()
        return status

    status = asyncio.run(exercise())
    spans = captured[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(captured) == 1 and len(spans) == 2
    assert status["exported_traces"] == 2
    assert status["exported_batches"] == 1
    assert status["failed_traces"] == 0


def test_queue_deduplicates_and_drops_newest_on_backpressure():
    async def sender(*args):
        return 200

    async def exercise():
        queue = PortableTelemetryQueue(
            sender=sender, environ=_env(VERA_OTLP_QUEUE_SIZE="1"))
        trace = project_run_trace(_completed("one"))
        assert queue.offer(trace, key="same") is True
        assert queue.offer(trace, key="same") is False
        assert queue.offer(project_run_trace(_completed("two")), key="two") is False
        before = queue.status()
        await queue.close()
        return before

    status = asyncio.run(exercise())
    assert status["deduplicated"] == 1
    assert status["dropped"] == 1
    assert status["queue_depth"] == 1


def test_transport_failure_isolated_and_worker_continues():
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("secret collector error")
        return 200

    async def exercise():
        queue = PortableTelemetryQueue(
            sender=sender, environ=_env(VERA_OTLP_BATCH_SIZE="1",
                                        VERA_OTLP_RETRY_MAX="0"))
        queue.offer_run(_completed("fail"))
        queue.offer_run(_completed("pass"))
        await queue._queue.join()
        status = queue.status()
        await queue.close()
        return status

    status = asyncio.run(exercise())
    assert calls == 2
    assert status["failed_traces"] == 1
    assert status["exported_traces"] == 1
    assert status["worker_running"] is True


def test_transient_failure_retries_with_a_bounded_budget():
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        return 503 if calls < 3 else 200

    async def exercise():
        queue = PortableTelemetryQueue(
            sender=sender, environ=_env(VERA_OTLP_RETRY_MAX="2",
                                        VERA_OTLP_RETRY_BACKOFF_MS="0"))
        queue.offer_run(_completed("retry"))
        await queue._queue.join()
        status = queue.status()
        await queue.close()
        return status

    status = asyncio.run(exercise())
    assert calls == 3
    assert status["retries"] == 2
    assert status["exported_traces"] == 1
    assert status["failed_traces"] == 0


@pytest.mark.parametrize("status_code", [400, 401, 403, 408, 500])
def test_non_retryable_otlp_statuses_are_attempted_once(status_code):
    calls = 0

    async def sender(*args):
        nonlocal calls
        calls += 1
        return status_code

    async def exercise():
        queue = PortableTelemetryQueue(
            sender=sender, environ=_env(VERA_OTLP_RETRY_MAX="3",
                                        VERA_OTLP_RETRY_BACKOFF_MS="0"))
        queue.offer_run(_completed(f"status-{status_code}"))
        await queue._queue.join()
        status = queue.status()
        await queue.close()
        return status

    result = asyncio.run(exercise())
    assert calls == 1
    assert result["retries"] == 0 and result["failed_traces"] == 1


def test_offer_is_nonblocking_and_fails_closed_without_running_loop():
    queue = PortableTelemetryQueue(environ=_env())
    assert queue.offer(project_run_trace(_completed("no-loop")), key="no-loop") is False
    assert queue.status()["last_result"] == "no_running_loop"
    assert queue.status()["dropped"] == 1


def test_only_terminal_root_runs_are_automatically_eligible():
    queue = PortableTelemetryQueue(environ=_env())
    running = Run(id="running", kind="run", status=RunStatus.RUNNING)
    child = Run(id="child", kind="run", parent_run_id="root",
                status=RunStatus.COMPLETED, ended_at="2026-01-01T00:00:00Z")
    assert queue.offer_run(running) is False
    assert queue.offer_run(child) is False
    assert queue.status()["offered"] == 0


def test_shadow_registry_enqueues_one_complete_root_trace_with_children(monkeypatch):
    from vera.execution import portable_telemetry
    from vera.execution import run_projection

    captured = []

    async def sender(endpoint, body, headers, timeout):
        captured.append(json.loads(body))
        return 200

    async def exercise():
        queue = PortableTelemetryQueue(sender=sender, environ=_env())
        monkeypatch.setattr(portable_telemetry, "TELEMETRY_QUEUE", queue)
        monkeypatch.setattr(run_projection, "_AUTO_TELEMETRY_ENABLED", True)
        registry = ShadowRunRegistry()
        root = Run(id="root", kind="vera.dag", trace_id="trace")
        registry.record(root, root.transition(RunStatus.RUNNING))
        child = Run(id="child", kind="vera.dag.node", parent_run_id="root",
                    trace_id="trace")
        registry.record(child, child.transition(RunStatus.RUNNING))
        registry.record(child, child.transition(RunStatus.COMPLETED))
        registry.record(root, root.transition(RunStatus.COMPLETED))
        await queue._queue.join()
        status = queue.status()
        await queue.close()
        return status

    status = asyncio.run(exercise())
    spans = captured[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(captured) == 1 and len(spans) == 2
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert status["enqueued"] == 1


def test_disabled_registry_path_does_not_touch_telemetry_queue(monkeypatch):
    from vera.execution import portable_telemetry, run_projection

    touched = False

    class BombQueue:
        def offer_run(self, *args):
            nonlocal touched
            touched = True
            raise AssertionError("disabled path touched exporter")

    monkeypatch.setattr(portable_telemetry, "TELEMETRY_QUEUE", BombQueue())
    monkeypatch.setattr(run_projection, "_AUTO_TELEMETRY_ENABLED", False)
    registry = ShadowRunRegistry()
    root = Run(id="disabled-root", kind="vera.dag")
    registry.record(root, root.transition(RunStatus.RUNNING))
    registry.record(root, root.transition(RunStatus.COMPLETED))
    assert touched is False
