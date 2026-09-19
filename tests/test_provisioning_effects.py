import json

import pytest

import Vera.vera.integrations.infrastructure_effects as effects
import Vera.vera.provisioning.components_capabilities as components
import Vera.vera.provisioning.security_provision_capabilities as security
import Vera.vera.provisioning.software_capabilities as software


pytestmark = pytest.mark.critical


def _shadow(kwargs):
    return {"enforcement": "observe_only", "delivery": {"mode": kwargs["mode"]}}


def test_provisioning_shadow_is_payload_free():
    shadow = effects.plan_infrastructure_effect(
        provider="ssh", target_ref="host:private", resource_ref="runtime:private",
        operation_ref="secret install command", mode="runtime_install",
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    encoded = json.dumps(shadow)
    assert shadow["delivery"]["provider"] == "ssh"
    for raw in ("host:private", "runtime:private", "secret install command",
                "key:private", "approval:private"):
        assert raw not in encoded


@pytest.mark.asyncio
async def test_component_deploy_observes_before_ssh_without_forwarding_controls(monkeypatch):
    order = []
    calls = []
    observations = []

    async def host(_host_id):
        return {"id": "host:private", "host": "private.invalid", "user": "vera"}

    async def ssh(*args, **kwargs):
        order.append("provider")
        calls.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "stdout": "VERA_LAUNCHED", "stderr": ""}

    def observe(**kwargs):
        order.append("observe")
        observations.append(kwargs)
        return _shadow(kwargs)

    monkeypatch.setattr(components, "_host_rec", host)
    monkeypatch.setattr(components, "_read_local", lambda _rel: b"content")
    monkeypatch.setattr(components, "_ssh", ssh)
    monkeypatch.setattr(components, "observe_infrastructure_effect", observe)
    monkeypatch.setattr(components, "emit_event", lambda *_args: _async(None))

    result = await components.cap_deploy.__wrapped__(
        host_id="host:private", component="onnx_runtime", launch=False,
        idempotency_key="key:private", approval_receipt_ref="approval:private",
        retry=True)
    assert order[:2] == ["observe", "provider"]
    assert len(observations) == 1
    assert observations[0]["mode"] == "component_deploy"
    assert result["effect_shadow"]["enforcement"] == "observe_only"
    assert "key:private" not in json.dumps(calls)
    assert "approval:private" not in json.dumps(calls)


@pytest.mark.asyncio
async def test_runtime_install_observes_once_before_ssh(monkeypatch):
    order = []
    observations = []

    async def host(_host_id):
        return {"id": "host:private", "host": "private.invalid", "user": "vera"}

    async def ssh(*_args, **kwargs):
        order.append("provider")
        assert "idempotency_key" not in kwargs
        assert "approval_receipt_ref" not in kwargs
        return {"ok": True, "stdout": "VERA_PROVISION_DONE", "stderr": "", "rc": 0}

    def observe(**kwargs):
        order.append("observe")
        observations.append(kwargs)
        return _shadow(kwargs)

    monkeypatch.setattr(software, "_host_rec", host)
    monkeypatch.setattr(software, "_ssh", ssh)
    monkeypatch.setattr(software, "observe_infrastructure_effect", observe)
    monkeypatch.setattr(software, "emit_event", lambda *_args: _async(None))
    result = await software.cap_install.__wrapped__(
        host_id="host:private", target="docker", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    assert order[:2] == ["observe", "provider"]
    assert len(observations) == 1
    assert observations[0]["mode"] == "runtime_install"
    assert result["effect_shadow"]["delivery"]["mode"] == "runtime_install"


@pytest.mark.asyncio
async def test_runtime_run_uses_one_public_effect_for_nested_install(monkeypatch):
    order = []
    observations = []

    async def host(_host_id):
        return {"id": "host:private", "host": "private.invalid", "user": "vera"}

    async def ssh(*_args, **_kwargs):
        order.append("provider")
        return {"ok": True, "stdout": "VERA_PROVISION_DONE", "stderr": "", "rc": 0}

    def observe(**kwargs):
        order.append("observe")
        observations.append(kwargs)
        return _shadow(kwargs)

    monkeypatch.setattr(software, "_host_rec", host)
    monkeypatch.setattr(software, "_ssh", ssh)
    monkeypatch.setattr(software, "observe_infrastructure_effect", observe)
    monkeypatch.setattr(software, "emit_event", lambda *_args: _async(None))
    result = await software.cap_run.__wrapped__(
        host_id="host:private", target="nvidia", connect=False,
        idempotency_key="key:private", approval_receipt_ref="approval:private",
        retry=True)
    assert order[:2] == ["observe", "provider"]
    assert len(observations) == 1
    assert observations[0]["mode"] == "runtime_run"
    assert result["effect_shadow"]["delivery"]["mode"] == "runtime_run"
    assert "effect_shadow" not in result["install"]


@pytest.mark.asyncio
async def test_running_security_service_is_noop_without_observation(monkeypatch):
    class Docker:
        _get_host = staticmethod(lambda _host_id: {"id": "host:private", "kind": "local"})

    monkeypatch.setattr(security, "_dk", lambda: Docker)
    monkeypatch.setattr(security, "_host_addr", lambda *_args: _async("localhost"))
    monkeypatch.setattr(security, "_container_state",
                        lambda *_args: _async({"State": "running"}))
    monkeypatch.setattr(
        security, "observe_infrastructure_effect",
        lambda **_kwargs: pytest.fail("no mutation was attempted"))
    result = await security.cap_sec_deploy.__wrapped__(service="opa")
    assert result["services"]["opa"]["already"] is True
    assert "effect_shadow" not in result["services"]["opa"]


@pytest.mark.asyncio
async def test_security_deploy_delegates_one_selected_effect_to_native_boundary(monkeypatch):
    order = []
    calls = []

    class Docker:
        _get_host = staticmethod(lambda _host_id: {"id": "host:private", "kind": "local"})

        @staticmethod
        async def _run_container_native(**kwargs):
            order.append("provider")
            calls.append(kwargs)
            return {"ok": True, "container_id": "cid",
                    "effect_shadow": kwargs["effect_shadow"]}

    def observe(**kwargs):
        order.append("observe")
        return _shadow(kwargs)

    monkeypatch.setattr(security, "_dk", lambda: Docker)
    monkeypatch.setattr(security, "_host_addr", lambda *_args: _async("localhost"))
    monkeypatch.setattr(security, "_container_state", lambda *_args: _async(None))
    monkeypatch.setattr(security, "_redis", lambda: None)
    monkeypatch.setattr(security, "observe_infrastructure_effect", observe)
    monkeypatch.setattr(security, "emit_event", lambda *_args: _async(None))
    result = await security.cap_sec_deploy.__wrapped__(
        service="opa", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    assert order[:2] == ["observe", "provider"]
    assert len(calls) == 1
    assert calls[0]["idempotency_key"] == "key:private"
    assert calls[0]["approval_receipt_ref"] == "approval:private"
    assert calls[0]["effect_mode"] == "security_deploy"
    assert result["services"]["opa"]["effect_shadow"]["delivery"]["mode"] == "security_deploy"


@pytest.mark.asyncio
async def test_security_remove_observes_before_docker(monkeypatch):
    order = []

    class Docker:
        _get_host = staticmethod(lambda _host_id: {"id": "host:private", "kind": "local"})

        @staticmethod
        async def _docker_argv(_rec, args):
            return ["docker", *args]

        @staticmethod
        async def _run_local(_argv, timeout):
            order.append("provider")
            return {"ok": True, "stderr": ""}

    def observe(**kwargs):
        order.append("observe")
        return _shadow(kwargs)

    monkeypatch.setattr(security, "_dk", lambda: Docker)
    monkeypatch.setattr(security, "_redis", lambda: None)
    monkeypatch.setattr(security, "observe_infrastructure_effect", observe)
    monkeypatch.setattr(security, "emit_event", lambda *_args: _async(None))
    result = await security.cap_sec_remove.__wrapped__(
        service="opa", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    assert order == ["observe", "provider"]
    assert result["effect_shadow"]["delivery"]["mode"] == "security_remove"


async def _async(value):
    return value
