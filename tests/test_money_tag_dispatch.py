"""A capability tagged "money" runs through the real dispatcher wrapper only from
its own HTTP route: a loop, a nested capability, background work, a bridge,
/mcp/call and a plain in-process call (schedule, widget) are refused before the
function runs. The real broker caps carry the tag and are in the Loop Lab test
denylist.

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)

PROBE = "test.money_guard.probe"
OUTER = "test.money_guard.outer"
RAN = []


@orch.capability(PROBE, tags=["test", "money"], memory="off", silent=True, mcp_expose=False)
async def _probe(trace_id=None):
    RAN.append(1)
    return {"ok": True, "placed": True}


@orch.capability(OUTER, memory="off", silent=True, mcp_expose=False)
async def _outer(trace_id=None):
    return await orch.CAPABILITY_REGISTRY[PROBE]["func"]()


def call(name=PROBE, **ctx):
    """Run ``name`` through the registry wrapper with the given context variables set."""
    tokens = []
    for var, value in ctx.items():
        cv = getattr(orch, var)
        tokens.append((cv, cv.set(value)))
    try:
        return asyncio.run(orch.CAPABILITY_REGISTRY[name]["func"]())
    finally:
        for cv, tok in reversed(tokens):
            cv.reset(tok)


@pytest.fixture(autouse=True)
def _clear():
    RAN.clear()
    yield


def test_own_route_runs_it():
    assert call(CURRENT_HTTP_CAP=PROBE) == {"ok": True, "placed": True}
    assert RAN == [1]


@pytest.mark.parametrize("ctx", [
    {},                                                               # in-process: schedule, trigger, widget
    {"CURRENT_HTTP_CAP": "some.other.cap"},
    {"CURRENT_HTTP_CAP": PROBE, "RUNNING_CAPS": ("dag.agent_loop_v8",)},
    {"CURRENT_HTTP_CAP": PROBE, "BACKGROUND_LLM": "dream:c1"},
    {"MCP_CALL_ACTIVE": True},                                        # browser /mcp/call (cap-in-chat)
    {"MCP_CALL_ACTIVE": True, "CALLER_KIND": "claude"},              # an MCP bridge
])
def test_other_callers_are_refused_before_it_runs(ctx):
    r = call(**ctx)
    assert r["ok"] is False and r["refused"] == "money" and r["capability"] == PROBE
    assert RAN == []


def test_a_capability_that_calls_it_is_refused_even_from_its_own_route():
    r = call(OUTER, CURRENT_HTTP_CAP=OUTER)
    assert r["refused"] == "money" and OUTER in r["error"]
    assert RAN == []


def test_the_real_broker_caps_carry_the_tag():
    import Vera.vera.markets.markets_capabilities  # noqa: F401  (registers markets.broker.*)
    reg = orch.CAPABILITY_REGISTRY
    for name in ("markets.broker.arm", "markets.broker.set_trading",
                 "markets.broker.limits.set", "markets.broker.order"):
        assert "money" in (reg[name].get("tags") or []), name
    for name in ("markets.broker.list", "markets.broker.balances", "markets.broker.providers"):
        assert "money" not in (reg[name].get("tags") or []), name


def test_loop_lab_tests_never_call_broker_caps():
    from Vera.vera.evolve.evolve_capabilities import DEFAULT_TEST_DENYLIST
    assert "markets.broker." in DEFAULT_TEST_DENYLIST
