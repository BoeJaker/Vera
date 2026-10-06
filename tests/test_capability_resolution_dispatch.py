import asyncio

import pytest

from vera.capability_resolution_dispatch import resolve_authorize_dispatch


def _entry(task, func, *, effects=None, required=None):
    return {
        "func": func,
        "schema": {"type": "object", "properties": {"value": {"type": "string"}},
                   "required": list(required or [])},
        "contract": {
            "canonical_task": task, "effects": list(effects or ["read"]),
            "output_schema": {"type": "object"}, "approval": {"status": "not_required"},
            "trust": {"status": "internal_registry"}, "secrets": {"status": "not_required"},
            "filesystem": {"status": "not_required"}, "network": {"status": "not_required"},
            "tenant": {"status": "global_read_only"}, "idempotency": {"status": "idempotent"},
            "cancellation": {"status": "not_required"}, "pagination": {"status": "bounded"},
            "resources": {"status": "declared", "classes": ["cpu"]}, "owner": "vera",
        },
    }


def _registry(call):
    async def coverage(**kwargs):
        call.append(("coverage", kwargs))
        return {"value": kwargs.get("value", "")}
    async def gate(**kwargs):
        call.append(("gate", kwargs))
        return {}
    async def observations(**kwargs):
        call.append(("observations", kwargs))
        return {}
    return {
        "cap.contract.coverage": _entry("capability.contract.coverage", coverage),
        "cap.contract.gate": _entry("capability.contract.gate", gate),
        "cap.contract.observations": _entry("capability.contract.observe", observations),
    }


@pytest.mark.critical
def test_dispatches_exactly_once_after_resolution_and_policy_allow():
    calls = []
    result = asyncio.run(resolve_authorize_dispatch(
        _registry(calls), "capability.contract.coverage", {"value": "x"},
        enabled=True, trace_id="trace"))
    assert result["ok"] and result["selected"] == "cap.contract.coverage"
    assert calls == [("coverage", {"value": "x", "trace_id": "trace"})]


@pytest.mark.critical
@pytest.mark.parametrize("enabled,task,error", [
    (False, "capability.contract.coverage", "dispatch_disabled"),
    (True, "provider.model.invoke", "task_not_migrated"),
])
def test_kill_switch_and_allowlist_fail_closed(enabled, task, error):
    calls = []
    result = asyncio.run(resolve_authorize_dispatch(_registry(calls), task, {}, enabled=enabled))
    assert result["error"] == error and not result["executed"] and calls == []


@pytest.mark.critical
@pytest.mark.parametrize("arguments,error", [
    ({"unknown": 1}, "arguments_unknown"),
    ({"trace_id": "forged"}, "arguments_reserved"),
    ([], "arguments_not_object"),
])
def test_argument_boundary_rejects_unknown_and_reserved_fields(arguments, error):
    calls = []
    result = asyncio.run(resolve_authorize_dispatch(
        _registry(calls), "capability.contract.coverage", arguments, enabled=True))
    assert result["error"] == error and calls == []


@pytest.mark.critical
def test_contract_drift_is_denied_before_dispatch():
    calls = []
    registry = _registry(calls)
    registry["cap.contract.coverage"]["contract"]["effects"] = ["write"]
    result = asyncio.run(resolve_authorize_dispatch(
        registry, "capability.contract.coverage", {}, enabled=True))
    assert result["error"] == "resolution_drift"
    assert calls == []


@pytest.mark.critical
def test_missing_required_argument_is_denied():
    calls = []
    registry = _registry(calls)
    registry["cap.contract.coverage"]["schema"]["required"] = ["value"]
    result = asyncio.run(resolve_authorize_dispatch(
        registry, "capability.contract.coverage", {}, enabled=True))
    assert result["error"] == "arguments_required" and calls == []


@pytest.mark.critical
def test_target_failure_is_not_retried_or_hidden():
    calls = []
    registry = _registry(calls)
    async def fail(**kwargs):
        calls.append(("failure", kwargs))
        raise RuntimeError("target failed")
    registry["cap.contract.coverage"]["func"] = fail
    with pytest.raises(RuntimeError, match="target failed"):
        asyncio.run(resolve_authorize_dispatch(
            registry, "capability.contract.coverage", {}, enabled=True))
    assert len(calls) == 1


@pytest.mark.critical
def test_dispatch_does_not_replace_direct_name_callable():
    calls = []
    registry = _registry(calls)
    direct = registry["cap.contract.coverage"]["func"]
    asyncio.run(resolve_authorize_dispatch(
        registry, "capability.contract.coverage", {}, enabled=True))
    assert registry["cap.contract.coverage"]["func"] is direct
    assert asyncio.run(direct(value="direct")) == {"value": "direct"}
