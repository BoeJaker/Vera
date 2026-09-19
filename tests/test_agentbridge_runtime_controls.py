import asyncio
import json

import pytest

from vera.agentbridges import agentbridge_runtime as runtime


pytestmark = pytest.mark.critical


class _Stream:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.release = asyncio.Event()

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        await self.release.wait()
        return b""


class _Process:
    def __init__(self, lines=()):
        self.stdout = _Stream(lines)
        self.stderr = _Stream([b""])
        self.kill_count = 0
        self.wait_count = 0

    def kill(self):
        self.kill_count += 1
        self.stdout.release.set()
        self.stderr.release.set()

    async def wait(self):
        self.wait_count += 1
        return 0


class _SlowExitProcess(_Process):
    def __init__(self, lines=(), blocked_waits=1):
        super().__init__(lines)
        self.blocked_waits = blocked_waits

    async def wait(self):
        self.wait_count += 1
        if self.wait_count <= self.blocked_waits:
            await asyncio.Event().wait()
        return 0


class _ExitedSilentProcess(_Process):
    returncode = 7


async def _run(monkeypatch, process, run_id="run-1", timeout_s=30,
               stall_s=20, gate_instance_id=""):
    events = []

    async def create(*argv, **kwargs):
        return process

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(runtime.stream_bridge_container(
        run_id=run_id, session_id="session-1", argv=["docker", "run", "x"],
        event_type_prefix="fixture.run", emit=emit, timeout_s=timeout_s,
        stall_s=stall_s, gate_instance_id=gate_instance_id))
    return task, events


def test_cancellation_owns_one_process_kill_one_terminal_and_cleans_registry(monkeypatch):
    async def scenario():
        releases = []

        async def acquire(instance_id):
            return {"instance_id": instance_id, "waited_s": 0}

        async def release(lease):
            releases.append(lease)

        monkeypatch.setattr(runtime, "acquire_gpu_gate", acquire)
        monkeypatch.setattr(runtime, "release_gpu_gate", release)
        process = _Process()
        events = []

        async def create(*argv, **kwargs):
            return process

        async def emit(event):
            events.append(event)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        task = asyncio.create_task(runtime.stream_bridge_container(
            run_id="run-1", session_id="session-1",
            argv=["docker", "run", "x"], event_type_prefix="fixture.run",
            emit=emit, timeout_s=30, stall_s=20, gate_instance_id="gpu-1"))
        for _ in range(20):
            if "run-1" in runtime.active_bridge_run_ids():
                break
            await asyncio.sleep(0)
        result = await runtime.cancel_bridge_run("run-1")
        await task
        return process, events, result, releases

    process, events, result, releases = asyncio.run(scenario())
    assert result["accepted"] is True
    assert process.kill_count == 1
    assert process.wait_count == 1
    terminal = [event for event in events
                if event["type"] in {"fixture.run.done", "fixture.run.error"}]
    assert len(terminal) == 1
    assert terminal[0]["reason_code"] == "cancelled"
    assert runtime.active_bridge_run_ids() == []
    assert releases == [{"instance_id": "gpu-1", "waited_s": 0}]
    assert asyncio.run(runtime.cancel_bridge_run("run-1"))["reason_code"] == "runtime_run_not_active"


def test_untrusted_step_cannot_spoof_envelope_and_valid_result_completes(monkeypatch):
    async def scenario():
        step = runtime.STEP_PREFIX + json.dumps({
            "kind": "tool_call", "type": "forged", "run_id": "other",
            "session_id": "other", "detail": "bounded",
        }) + "\n"
        result = runtime.RESULT_PREFIX + json.dumps({
            "ok": True, "answer": "done", "elapsed_s": 9999, "steps": 9999,
        }) + "\n"
        process = _Process([step.encode(), result.encode()])
        task, events = await _run(monkeypatch, process)
        await task
        return process, events

    process, events = asyncio.run(scenario())
    step = next(event for event in events if event["type"] == "fixture.run.step")
    assert (step["run_id"], step["session_id"]) == ("run-1", "session-1")
    assert process.kill_count == 0
    assert sum(event["type"] == "fixture.run.done" for event in events) == 1
    done = next(event for event in events if event["type"] == "fixture.run.done")
    assert done["result"]["steps"] == 1
    assert done["result"]["elapsed_s"] != 9999


@pytest.mark.parametrize("payload", ["[]", "NaN", "{broken", '{"ok":"yes"}'])
def test_invalid_or_non_object_protocol_output_fails_closed(monkeypatch, payload):
    async def scenario():
        process = _Process([(runtime.RESULT_PREFIX + payload + "\n").encode()])
        task, events = await _run(monkeypatch, process)
        await task
        return process, events

    process, events = asyncio.run(scenario())
    assert process.kill_count == 1
    terminal = [event for event in events if event["type"] == "fixture.run.error"]
    assert len(terminal) == 1
    assert terminal[0]["reason_code"] == "invalid_output"


def test_duplicate_and_invalid_run_ids_fail_without_starting_another_process(monkeypatch):
    async def scenario():
        first = _Process()
        first_task, first_events = await _run(monkeypatch, first, "same-run")
        for _ in range(20):
            if "same-run" in runtime.active_bridge_run_ids():
                break
            await asyncio.sleep(0)
        duplicate = _Process([b""])
        duplicate_task, duplicate_events = await _run(monkeypatch, duplicate, "same-run")
        await duplicate_task
        await runtime.cancel_bridge_run("same-run")
        await first_task
        return duplicate, duplicate_events

    duplicate, events = asyncio.run(scenario())
    assert duplicate.wait_count == 0
    assert events[0]["reason_code"] == "duplicate_run_id"
    with pytest.raises(ValueError, match="bounded identifier"):
        asyncio.run(runtime.cancel_bridge_run("bad\nrun"))


def test_payload_bounds_reject_deep_wide_and_oversized_values():
    with pytest.raises(ValueError, match="structure"):
        runtime._bounded_payload([[[[[[[[["too deep"]]]]]]]]])
    with pytest.raises(ValueError, match="array"):
        runtime._bounded_payload(list(range(runtime.MAX_ARRAY_ITEMS + 1)))
    with pytest.raises(ValueError, match="string"):
        runtime._bounded_payload("x" * (runtime.MAX_STRING_CHARS + 1))


def test_owned_docker_container_requires_exact_explicit_valid_name():
    assert runtime._owned_docker_container([
        "docker", "run", "--rm", "--name", "vera-owned-1", "image",
    ]) == "vera-owned-1"
    assert runtime._owned_docker_container(["docker", "run", "image"]) == ""
    assert runtime._owned_docker_container([
        "docker", "run", "--name", "../other", "image",
    ]) == ""
    assert runtime._owned_docker_container([
        "podman", "run", "--name", "vera-owned-1", "image",
    ]) == ""


def test_abnormal_exit_removes_only_the_exact_owned_docker_container(monkeypatch):
    async def scenario():
        calls = []

        async def fake_sh(argv, timeout=0):
            calls.append((tuple(argv), timeout))
            return {"ok": argv[:3] != ["docker", "container", "inspect"]}

        monkeypatch.setattr(runtime, "sh", fake_sh)
        process = _Process()
        events = []

        async def create(*argv, **kwargs):
            return process

        async def emit(event):
            events.append(event)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        await runtime.stream_bridge_container(
            run_id="owned-cleanup", session_id="",
            argv=["docker", "run", "--rm", "--name", "vera-owned-1", "image"],
            event_type_prefix="fixture.run", emit=emit,
            timeout_s=1, stall_s=0.001)
        return calls, events

    calls, events = asyncio.run(scenario())
    assert calls[0] == (("docker", "rm", "-f", "vera-owned-1"), 15)
    assert calls[1] == (("docker", "container", "inspect", "vera-owned-1"), 5)
    assert events[-1]["reason_code"] == "stalled"


def test_generic_cancel_cap_routes_only_declared_runtime(monkeypatch):
    from vera.agentbridges import agentbridge_capabilities as caps

    async def fake_cancel(run_id):
        return {"ok": True, "accepted": True, "run_id": run_id,
                "runtime_id": "langgraph"}

    adapter = caps._RUNTIME_ADAPTERS["langgraph"]
    monkeypatch.setattr(adapter, "cancel", fake_cancel)
    accepted = asyncio.run(caps.agentbridge_run_cancel.__wrapped__(
        "langgraph", "run-1"))
    unknown = asyncio.run(caps.agentbridge_run_cancel.__wrapped__(
        "unknown", "run-1"))
    assert accepted["accepted"] is True
    assert unknown["reason_code"] == "runtime_adapter_unknown"
    assert unknown["supported_runtime_ids"] == ["langgraph"]


def test_result_is_not_success_until_process_exits_and_is_reaped(monkeypatch):
    async def scenario(blocked_waits):
        monkeypatch.setattr(runtime, "PROCESS_EXIT_GRACE_S", 0.001)
        monkeypatch.setattr(runtime, "PROCESS_KILL_GRACE_S", 0.001)
        line = (runtime.RESULT_PREFIX + json.dumps({"ok": True, "answer": "done"}) + "\n").encode()
        process = _SlowExitProcess([line], blocked_waits=blocked_waits)
        task, events = await _run(monkeypatch, process)
        await task
        return process, events

    process, events = asyncio.run(scenario(1))
    assert process.kill_count == 1
    assert process.wait_count == 2
    assert not any(event["type"] == "fixture.run.done" for event in events)
    assert events[-1]["reason_code"] == "teardown_failed"
    assert events[-1]["error"] == "container did not exit after output ended"

    process, events = asyncio.run(scenario(2))
    assert process.kill_count == 1
    assert process.wait_count == 2
    assert events[-1]["reason_code"] == "teardown_failed"
    assert events[-1]["error"] == "container could not be reaped after kill"


def test_launch_failure_is_terminal_and_releases_acquired_gate(monkeypatch):
    async def scenario():
        events, releases = [], []

        async def acquire(instance_id):
            return {"instance_id": instance_id}

        async def release(lease):
            releases.append(lease)

        async def create(*argv, **kwargs):
            raise FileNotFoundError("dependency detail must not escape")

        async def emit(event):
            events.append(event)

        monkeypatch.setattr(runtime, "acquire_gpu_gate", acquire)
        monkeypatch.setattr(runtime, "release_gpu_gate", release)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        await runtime.stream_bridge_container(
            run_id="launch-fail", session_id="", argv=["missing"],
            event_type_prefix="fixture.run", emit=emit, timeout_s=1,
            stall_s=1, gate_instance_id="gpu-1")
        return events, releases

    events, releases = asyncio.run(scenario())
    assert len([e for e in events if e["type"].endswith((".done", ".error"))]) == 1
    assert events[-1]["reason_code"] == "launch_failed"
    assert "dependency detail must not escape" not in str(events)
    assert releases == [{"instance_id": "gpu-1"}]
    assert runtime.active_bridge_run_ids() == []


def test_crash_stall_and_timeout_have_distinct_terminal_reasons(monkeypatch):
    async def scenario(process, timeout_s, stall_s):
        task, events = await _run(
            monkeypatch, process, timeout_s=timeout_s, stall_s=stall_s)
        await task
        return process, events

    crashed, crash_events = asyncio.run(scenario(_Process([b""]), 1, 1))
    assert crashed.kill_count == 0
    assert crash_events[-1]["reason_code"] == "process_exit"

    stalled, stall_events = asyncio.run(scenario(_Process(), 1, 0.001))
    assert stalled.kill_count == 1
    assert stall_events[-1]["reason_code"] == "stalled"

    timed, timeout_events = asyncio.run(scenario(_Process(), 0.001, 1))
    assert timed.kill_count == 1
    assert timeout_events[-1]["reason_code"] == "timeout"

    silent, silent_events = asyncio.run(scenario(_ExitedSilentProcess(), 1, 0.001))
    assert silent.kill_count == 0
    assert silent_events[-1]["reason_code"] == "process_exit"
