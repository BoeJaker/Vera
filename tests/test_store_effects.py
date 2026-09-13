import json

import pytest

import Vera.vera.provisioning.stores_capabilities as stores
import Vera.vera.workers.docker_capabilities as docker
import Vera.vera.workers.docker_effects as effects


pytestmark = pytest.mark.critical


def _host():
    return {"id": "host:private", "kind": "local"}


@pytest.mark.parametrize("mode", ["stack_deploy", "store_deploy", "store_remove"])
def test_store_modes_are_payload_free(mode):
    shadow = effects.plan_docker_effect(
        host_ref="host:private", resource_ref="store:private",
        operation_ref="secret deployment inputs", mode=mode,
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    encoded = json.dumps(shadow)
    assert shadow["delivery"]["mode"] == mode
    for raw in ("host:private", "store:private", "secret deployment inputs",
                "key:private", "approval:private"):
        assert raw not in encoded


@pytest.mark.asyncio
async def test_native_container_helper_emits_one_selected_effect(monkeypatch):
    observed = []
    monkeypatch.setattr(docker, "_get_host", lambda _host_id: _host())
    monkeypatch.setattr(docker, "_sandbox_gate", lambda *_args: (True, ""))

    async def argv(_rec, args):
        return ["docker", *args]

    async def run(_argv, timeout):
        return {"ok": True, "stdout": "container-id", "stderr": ""}

    async def emit(_event):
        return None

    def observe(**kwargs):
        observed.append(kwargs)
        return {"delivery": {"mode": kwargs["mode"]}}

    monkeypatch.setattr(docker, "_docker_argv", argv)
    monkeypatch.setattr(docker, "_run_local", run)
    monkeypatch.setattr(docker, "emit_event", emit)
    monkeypatch.setattr(docker, "observe_docker_effect", observe)
    result = await docker._run_container_native(
        host_id="host:private", image="image:private", name="store:private",
        effect_mode="store_deploy", idempotency_key="key:private",
        approval_receipt_ref="approval:private")
    assert [row["mode"] for row in observed] == ["store_deploy"]
    assert result["effect_shadow"]["delivery"]["mode"] == "store_deploy"


@pytest.mark.asyncio
async def test_running_store_is_a_noop_without_observation(monkeypatch):
    class Docker:
        _get_host = staticmethod(lambda _host_id: _host())

    monkeypatch.setattr(stores, "_dk", lambda: Docker)
    monkeypatch.setattr(stores, "_host_addr", lambda *_args: _async("localhost"))
    monkeypatch.setattr(stores, "_container_state",
                        lambda *_args: _async({"State": "running"}))
    monkeypatch.setattr(stores, "observe_docker_effect",
                        lambda **_kwargs: pytest.fail("no mutation was attempted"))
    result = await stores.cap_store_deploy.__wrapped__(store="postgres")
    assert result["stores"]["postgres"]["already"] is True
    assert "effect_shadow" not in result["stores"]["postgres"]


@pytest.mark.asyncio
async def test_stopped_store_observes_after_gate_before_restart(monkeypatch):
    order = []

    class Docker:
        _get_host = staticmethod(lambda _host_id: _host())
        _sandbox_gate = staticmethod(lambda *_args: (order.append("gate") or True, ""))

        @staticmethod
        async def _docker_argv(_rec, args):
            return ["docker", *args]

        @staticmethod
        async def _run_local(argv, timeout):
            order.append("run")
            assert "key:private" not in argv
            assert "approval:private" not in argv
            return {"ok": True}

    monkeypatch.setattr(stores, "_dk", lambda: Docker)
    monkeypatch.setattr(stores, "_host_addr", lambda *_args: _async("localhost"))
    monkeypatch.setattr(stores, "_container_state",
                        lambda *_args: _async({"State": "exited"}))

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["mode"] == "store_deploy"
        assert kwargs["idempotency_key"] == "key:private"
        return {"enforcement": "observe_only"}

    monkeypatch.setattr(stores, "observe_docker_effect", observe)
    result = await stores.cap_store_deploy.__wrapped__(
        store="postgres", idempotency_key="key:private",
        approval_receipt_ref="approval:private")
    assert order == ["gate", "observe", "run"]
    assert result["stores"]["postgres"]["effect_shadow"] == {
        "enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_absent_store_uses_one_logical_native_effect(monkeypatch):
    calls = []

    class Docker:
        _get_host = staticmethod(lambda _host_id: _host())

        @staticmethod
        async def _run_container_native(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "container_id": "cid",
                    "effect_shadow": {"delivery": {"mode": kwargs["effect_mode"]}}}

    monkeypatch.setattr(stores, "_dk", lambda: Docker)
    monkeypatch.setattr(stores, "_host_addr", lambda *_args: _async("localhost"))
    monkeypatch.setattr(stores, "_container_state", lambda *_args: _async(None))
    monkeypatch.setattr(stores, "_record_save", lambda *_args: _async(None))
    monkeypatch.setattr(stores, "emit_event", lambda *_args: _async(None))
    result = await stores.cap_store_deploy.__wrapped__(
        store="postgres", idempotency_key="key:private",
        approval_receipt_ref="approval:private")
    assert len(calls) == 1
    assert calls[0]["effect_mode"] == "store_deploy"
    assert calls[0]["effect_shadow"] is None
    assert calls[0]["idempotency_key"] == "key:private"
    assert result["stores"]["postgres"]["effect_shadow"]["delivery"]["mode"] == "store_deploy"


@pytest.mark.asyncio
async def test_store_remove_observes_once_before_any_docker_mutation(monkeypatch):
    order = []

    class Docker:
        _get_host = staticmethod(lambda _host_id: _host())
        _sandbox_gate = staticmethod(lambda *_args: (order.append("gate") or True, ""))

        @staticmethod
        async def _docker_argv(_rec, args):
            return ["docker", *args]

        @staticmethod
        async def _run_local(argv, timeout):
            order.append("run:" + argv[1])
            assert "key:private" not in argv
            assert "approval:private" not in argv
            return {"ok": True, "stderr": ""}

    monkeypatch.setattr(stores, "_dk", lambda: Docker)
    monkeypatch.setattr(stores, "_redis", lambda: None)

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["mode"] == "store_remove"
        return {"enforcement": "observe_only"}

    monkeypatch.setattr(stores, "observe_docker_effect", observe)
    monkeypatch.setattr(stores, "emit_event", lambda *_args: _async(None))
    result = await stores.cap_store_remove.__wrapped__(
        store="redis", purge_volumes=True, idempotency_key="key:private",
        approval_receipt_ref="approval:private")
    assert order[0:3] == ["gate", "observe", "run:rm"]
    assert order.count("observe") == 1
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_stack_wrapper_forwards_controls_only_to_canonical_store(monkeypatch):
    captured = {}

    class Stores:
        _STORES = {"redis": {}}

        @staticmethod
        async def cap_store_deploy(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "stores": {"redis": {
                "ok": True, "effect_shadow": {"enforcement": "observe_only"}}}}

    monkeypatch.setattr(docker, "_get_host", lambda _host_id: _host())
    monkeypatch.setattr(docker, "_stores_mod", lambda: Stores)
    result = await docker.cap_docker_stack_deploy.__wrapped__(
        host_id="host:private", service="redis",
        idempotency_key="key:private", approval_receipt_ref="approval:private",
        retry=True)
    assert captured["idempotency_key"] == "key:private"
    assert captured["approval_receipt_ref"] == "approval:private"
    assert captured["retry"] is True
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


async def _async(value):
    return value
