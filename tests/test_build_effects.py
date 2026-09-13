import json

import pytest

import Vera.vera.build.build_capabilities as build
import Vera.vera.integrations.infrastructure_effects as effects


pytestmark = pytest.mark.critical


def _plan(**overrides):
    arguments = {
        "provider": "builder", "target_ref": "builder:private",
        "resource_ref": "artifact:private", "operation_ref": "source:private",
        "mode": "run", "idempotency_key": "key:private",
        "approval_receipt_ref": "approval:private", "retry": True,
    }
    arguments.update(overrides)
    return effects.plan_infrastructure_effect(**arguments)


def test_infrastructure_plan_is_stable_payload_free_and_observe_only():
    assert _plan() == _plan()
    shadow = _plan()
    assert shadow["delivery"]["provider"] == "builder"
    assert shadow["delivery"]["mode"] == "run"
    assert shadow["blocks_current_call"] is False
    assert shadow["executes"] is False
    encoded = json.dumps(shadow)
    for raw in ("builder:private", "artifact:private", "source:private",
                "key:private", "approval:private"):
        assert raw not in encoded


@pytest.mark.parametrize("field", ["target_ref", "resource_ref", "operation_ref"])
def test_infrastructure_plan_requires_complete_identity(field):
    with pytest.raises(ValueError):
        _plan(**{field: ""})


@pytest.mark.parametrize("field", ["provider", "mode"])
def test_infrastructure_plan_rejects_unbounded_identity(field):
    with pytest.raises(ValueError, match="unsupported"):
        _plan(**{field: "BAD / private"})


@pytest.mark.asyncio
async def test_observed_builder_post_observes_once_before_remote_call(monkeypatch):
    order = []
    captured = {}

    def observe(**kwargs):
        order.append("observe")
        captured.update(kwargs)
        return {"enforcement": "observe_only"}

    async def post(path, payload, timeout):
        order.append("post")
        assert "key:private" not in json.dumps(payload)
        assert "approval:private" not in json.dumps(payload)
        return {"ok": False, "error": "unreachable"}

    monkeypatch.setattr(build, "_observe_build_effect", observe)
    monkeypatch.setattr(build, "builder_post", post)
    result = await build._observed_builder_post(
        path="/build/exec", payload={"command": "private"}, mode="run",
        resource_ref="build-command", idempotency_key="key:private",
        approval_receipt_ref="approval:private")
    assert order == ["observe", "post"]
    assert captured["idempotency_key"] == "key:private"
    assert captured["approval_receipt_ref"] == "approval:private"
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_build_run_keeps_controls_out_of_builder_payload(monkeypatch):
    captured = {}

    async def post(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "artifacts": {},
                "effect_shadow": {"delivery": {"mode": kwargs["mode"]}}}

    monkeypatch.setattr(build, "_observed_builder_post", post)
    result = await build.cap_build_run.__wrapped__(
        command="make private", files={"secret.txt": "private"},
        env={"TOKEN": "private"}, idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    assert captured["mode"] == "run"
    assert captured["idempotency_key"] == "key:private"
    assert captured["approval_receipt_ref"] == "approval:private"
    assert "idempotency_key" not in captured["payload"]
    assert "approval_receipt_ref" not in captured["payload"]
    assert result["effect_shadow"]["delivery"]["mode"] == "run"


@pytest.mark.asyncio
async def test_reachable_builder_is_noop_without_observation(monkeypatch):
    monkeypatch.setattr(build, "resolve_builder_url", lambda **_kw: _async("http://builder"))
    monkeypatch.setattr(build, "builder_get", lambda *_args, **_kw: _async({"tools": ["gcc"]}))
    monkeypatch.setattr(build, "_observe_build_effect",
                        lambda **_kw: pytest.fail("no mutation was attempted"))
    result = await build.cap_build_builder_up.__wrapped__(background=False)
    assert result["already"] is True
    assert "effect_shadow" not in result


@pytest.mark.asyncio
async def test_builder_image_build_observes_before_direct_mutation(monkeypatch):
    order = []

    class Probe:
        async def communicate(self):
            return b"", b""

    async def create(*_args, **_kwargs):
        order.append("probe")
        return Probe()

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["idempotency_key"] == "key:private"
        return {"enforcement": "observe_only"}

    async def stream(_jid, _argv, timeout):
        order.append("build")
        return 1

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    monkeypatch.setattr(build, "_observe_build_effect", observe)
    monkeypatch.setattr(build, "run_streaming", stream)
    jid = build.job_start("test")
    result = await build._builder_up_job(
        jid, 8785, True, "", 10, "key:private", "approval:private", False)
    assert order == ["probe", "observe", "build"]
    assert result["effect_shadow"] == {"enforcement": "observe_only"}


async def _async(value):
    return value
