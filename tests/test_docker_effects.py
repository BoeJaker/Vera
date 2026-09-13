import ast
import json
from pathlib import Path

import pytest

import Vera.vera.workers.docker_effects as effects
import Vera.vera.workers.docker_capabilities as docker


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def _plan(**overrides):
    arguments = {
        "host_ref": "private-host",
        "resource_ref": "private-container",
        "operation_ref": "private-command --token xyz-sensitive-value",
        "mode": "exec",
        "idempotency_key": "private-key",
        "approval_receipt_ref": "private-approval",
        "retry": True,
    }
    arguments.update(overrides)
    return effects.plan_docker_effect(**arguments)


def test_docker_plan_is_stable_payload_free_and_observe_only():
    first = _plan()
    assert first == _plan()
    assert first["enforcement"] == "observe_only"
    assert first["delivery"]["mode"] == "exec"
    assert first["delivery"]["provider_idempotency_forwarded"] is False
    assert first["blocks_current_call"] is False
    assert first["executes"] is False
    encoded = json.dumps(first).lower()
    for secret in ("private-host", "private-container", "private-command",
                   "xyz-sensitive-value", "private-key", "private-approval"):
        assert secret not in encoded


def test_operation_payload_changes_logical_effect_identity():
    assert _plan(operation_ref="echo one")["plan"]["plan_id"] != _plan(
        operation_ref="echo two")["plan"]["plan_id"]


@pytest.mark.parametrize("field", ["host_ref", "resource_ref", "operation_ref"])
def test_docker_plan_requires_complete_identity(field):
    with pytest.raises(ValueError):
        _plan(**{field: ""})


def test_docker_plan_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported"):
        _plan(mode="prune")


@pytest.mark.parametrize("mode", ["image_ensure", "worker_spawn"])
def test_image_and_worker_modes_use_distinct_effect_identities(mode):
    assert _plan(mode=mode)["delivery"]["mode"] == mode


def test_evidence_failure_is_isolated(monkeypatch):
    class BrokenEvidence:
        def record(self, _shadow):
            raise OSError("unavailable")

    monkeypatch.setattr(effects, "default_external_effect_shadow_evidence",
                        lambda **_kwargs: BrokenEvidence())
    arguments = {"host_ref": "h", "resource_ref": "r",
                 "operation_ref": "o", "mode": "stop"}
    expected = effects.plan_docker_effect(**arguments)
    observed = effects.observe_docker_effect(**arguments)
    assert observed["decision"] == expected["decision"]
    assert observed["blocks_current_call"] is False


def test_docker_capabilities_observe_after_local_gate_before_provider_execution():
    source = (ROOT / "vera" / "workers" / "docker_capabilities.py").read_text()
    run_start = source.index("async def _run_container_native")
    run = source[run_start:source.index("# ─────────────────────────────", run_start)]
    assert run.index("_sandbox_gate(") < run.index("observe_docker_effect(")
    assert run.index("observe_docker_effect(") < run.index("_run_local(argv")
    execute = source[source.index("async def cap_docker_exec"):
                     source.index('"docker.stop",')]
    assert execute.index("_sandbox_gate(") < execute.index("observe_docker_effect(")
    assert execute.index("observe_docker_effect(") < execute.index("_run_local(argv")


def test_controls_are_not_forwarded_to_docker_argv():
    source = (ROOT / "vera" / "workers" / "docker_capabilities.py").read_text()
    assert "idempotency_key=idempotency_key" in source
    assert "approval_receipt_ref=approval_receipt_ref" in source
    assert 'args += ["--idempotency-key"' not in source
    assert 'args += ["--approval-receipt"' not in source
    ast.parse(source, filename="docker_capabilities.py")


@pytest.mark.asyncio
async def test_exec_observes_before_docker_and_keeps_controls_out_of_argv(monkeypatch):
    order = []
    observed = {}

    monkeypatch.setattr(docker, "_get_host", lambda _host: {"id": "host:1"})
    monkeypatch.setattr(docker, "_sandbox_gate", lambda *_args: (True, ""))

    def observe(**kwargs):
        order.append("observe")
        observed.update(kwargs)
        return {"enforcement": "observe_only"}

    async def argv(_rec, args):
        order.append("argv")
        return ["docker", *args]

    async def run(args, timeout):
        order.append("run")
        assert "approval:private" not in args
        assert "key:private" not in args
        return {"ok": True, "rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(docker, "observe_docker_effect", observe)
    monkeypatch.setattr(docker, "_docker_argv", argv)
    monkeypatch.setattr(docker, "_run_local", run)
    result = await docker.cap_docker_exec.__wrapped__(
        host_id="host:1", container="c1", command="echo hello",
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    assert order == ["observe", "argv", "run"]
    assert observed["idempotency_key"] == "key:private"
    assert observed["approval_receipt_ref"] == "approval:private"
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_blocked_exec_neither_observes_nor_calls_docker(monkeypatch):
    monkeypatch.setattr(docker, "_get_host", lambda _host: {"id": "host:1"})
    monkeypatch.setattr(docker, "_sandbox_gate", lambda *_args: (False, "denied"))
    monkeypatch.setattr(docker, "observe_docker_effect",
                        lambda **_kwargs: pytest.fail("must not observe blocked call"))

    async def never(*_args, **_kwargs):
        pytest.fail("must not call Docker for blocked call")

    async def emit(_event):
        return None

    monkeypatch.setattr(docker, "_docker_argv", never)
    monkeypatch.setattr(docker, "_run_local", never)
    monkeypatch.setattr(docker, "emit_event", emit)
    result = await docker.cap_docker_exec.__wrapped__(
        host_id="host:1", container="c1", command="denied")
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_present_image_short_circuits_without_mutation_observation(monkeypatch):
    monkeypatch.setattr(docker, "_get_host", lambda _host: {"id": "local"})

    async def present(_rec, _image):
        return True

    monkeypatch.setattr(docker, "_image_present", present)
    monkeypatch.setattr(docker, "observe_docker_effect",
                        lambda **_kwargs: pytest.fail("no mutation was attempted"))
    result = await docker.cap_docker_image_ensure.__wrapped__(image="vera:test")
    assert result["action"] == "none"
    assert "effect_shadow" not in result


@pytest.mark.asyncio
async def test_image_build_observes_after_gate_before_docker(monkeypatch):
    order = []
    checks = iter((False, True))
    monkeypatch.setattr(docker, "_get_host", lambda _host: {"id": "local"})

    async def present(_rec, _image):
        return next(checks)

    async def argv(_rec, args):
        order.append("argv")
        return ["docker", *args]

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["idempotency_key"] == "key:private"
        assert kwargs["approval_receipt_ref"] == "approval:private"
        return {"enforcement": "observe_only"}

    async def run(args, timeout):
        order.append("run")
        assert "key:private" not in args
        assert "approval:private" not in args
        return {"ok": True, "stdout": "built", "stderr": ""}

    async def emit(_event):
        return None

    monkeypatch.setattr(docker, "_image_present", present)
    monkeypatch.setattr(docker, "_docker_argv", argv)
    monkeypatch.setattr(docker, "_sandbox_gate", lambda *_args: (True, ""))
    monkeypatch.setattr(docker, "observe_docker_effect", observe)
    monkeypatch.setattr(docker, "_run_local", run)
    monkeypatch.setattr(docker, "emit_event", emit)
    result = await docker.cap_docker_image_ensure.__wrapped__(
        image="vera:test", strategy="build", context="/workspace",
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    assert order == ["argv", "observe", "run"]
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_worker_spawn_observes_once_before_docker_without_forwarding_controls(monkeypatch):
    order = []
    monkeypatch.setattr(docker, "_get_host", lambda _host: {"id": "local"})

    async def argv(_rec, args):
        order.append("argv")
        return ["docker", *args]

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["mode"] == "worker_spawn"
        return {"enforcement": "observe_only"}

    async def run(args, timeout):
        order.append("run")
        assert "key:private" not in args
        assert "approval:private" not in args
        return {"ok": True, "stdout": "container-id", "stderr": ""}

    async def emit(_event):
        return None

    monkeypatch.setattr(docker, "_docker_argv", argv)
    monkeypatch.setattr(docker, "_sandbox_gate", lambda *_args: (True, ""))
    monkeypatch.setattr(docker, "observe_docker_effect", observe)
    monkeypatch.setattr(docker, "_run_local", run)
    monkeypatch.setattr(docker, "emit_event", emit)
    result = await docker.cap_docker_worker_spawn.__wrapped__(
        image="vera:test", name="worker-test", redis_url="redis://private",
        ensure_image=False, inherit_backends=False,
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    assert order == ["argv", "observe", "run"]
    assert result["effect_shadow"] == {"enforcement": "observe_only"}
