"""/mcp/call keeps an explicit caller's arguments to a **kwargs engine.

dag.agent_loop_v7 is (goal, **kwargs) and forwards to dag.agent_loop_v6; its
derived schema declares only `goal`, so /mcp/call's schema filter discarded
every other argument - found 2026-09-27 when four runs asked for four planning
styles and all ran as auto (census rows' enable_dream_persistence/model went the
same way). The filter now widens by the delegate, as engine_params does for
loops.run. App import, so it runs in-container where the gate runs.
"""
import pytest

try:
    from Vera.vera import capability_orchestration as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")


async def _engine(goal, **kwargs):
    return kwargs


_engine.delegates_to = "test.delegate_target"


async def _plain(goal: str = "", trace_id=None):
    return goal


@pytest.fixture
def registry(monkeypatch):
    reg = dict(M.CAPABILITY_REGISTRY)
    reg["test.delegate_target"] = {"func": _plain, "schema": {"properties": {
        "goal": {}, "plan_style": {}, "model": {}, "max_steps": {}}}}
    monkeypatch.setattr(M, "CAPABILITY_REGISTRY", reg)
    return reg


def test_a_kwargs_engine_accepts_what_its_delegate_accepts(registry):
    cap = {"func": _engine, "schema": {"properties": {"goal": {}}}}
    ok = M._mcp_call_accepted(cap)
    assert {"goal", "plan_style", "model", "max_steps", "session_id"} <= ok


def test_trace_id_is_never_passed_through_the_handler_supplies_it(registry):
    cap = {"func": _engine, "schema": {"properties": {"goal": {}}}}
    assert "trace_id" not in M._mcp_call_accepted(cap)


def test_a_complete_signature_keeps_its_own_filter(registry):
    cap = {"func": _plain, "schema": {"properties": {"goal": {}, "trace_id": {}}}}
    assert M._mcp_call_accepted(cap) == {"goal", "trace_id"}


def test_an_unknown_delegate_falls_back_to_the_own_schema(registry):
    async def _orphan(goal, **kwargs):
        return kwargs
    _orphan.delegates_to = "no.such.cap"
    cap = {"func": _orphan, "schema": {"properties": {"goal": {}}}}
    assert "plan_style" not in M._mcp_call_accepted(cap)


def test_no_schema_means_no_filtering_as_before(registry):
    assert M._mcp_call_accepted({"func": _plain}) == set()


def test_the_real_v7_accepts_plan_style():
    v7 = M.CAPABILITY_REGISTRY.get("dag.agent_loop_v7")
    if not v7:
        pytest.skip("loop module not loaded in this process")
    ok = M._mcp_call_accepted(v7)
    assert {"plan_style", "model", "enable_dream_persistence", "max_steps"} <= ok
