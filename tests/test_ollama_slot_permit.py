"""A leaked per-node generation permit wedges every later request on that node.

2026-08-29: ALL generation stopped - chat included - while ollama.gate.status
showed held=0 and the ollama panel showed no jobs queued except embeds. sysmon
still counted 8 reserved slots. The requests had reserved a routing slot and
were then blocked forever inside _ollama_slot, before anything was ever sent.

_ollama_slot takes a per-node semaphore whose limit is 1 (OLLAMA_CONCURRENCY),
with an unbounded `await sem.acquire()` by default. That acquire sat OUTSIDE
the try whose finally calls sem.release(), and the gate block in between awaits
Redis. A cancellation there raises CancelledError - a BaseException, so the gate
block's `except Exception` does not catch it - and the permit was never handed
back. One generation cancelled at the wrong moment wedged that node for the life
of the process. The in_use counter sweep cannot help: it reclaims the ROUTING
counter, not the semaphore permit.

Two guards: a structural one (pure, runs anywhere) pinning that nothing may be
awaited while the permit is held but outside the try, and a behavioural one that
cancels a real _ollama_slot inside the gate block and asserts the permit came back.
"""
import ast
import asyncio
import os

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py")


def _slot_fn():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ollama_slot":
            return node
    raise AssertionError("_ollama_slot not found - did it move or get renamed?")


def _mentions(node, dotted):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            if f"{sub.value.id}.{sub.attr}" == dotted:
                return True
    return False


def test_nothing_is_awaited_while_the_permit_is_held_outside_the_try():
    fn = _slot_fn()
    body = fn.body
    acquire_at = [i for i, st in enumerate(body) if _mentions(st, "sem.acquire")]
    assert acquire_at, "no sem.acquire in _ollama_slot"
    last_acquire = acquire_at[-1]

    guard_at = [i for i, st in enumerate(body)
                if isinstance(st, ast.Try)
                and any(_mentions(f, "sem.release") for f in st.finalbody)]
    assert guard_at, "no try/finally releasing the permit"
    guard = guard_at[0]
    assert guard > last_acquire, "the releasing try must come after the acquire"

    # The window between taking the permit and entering the try must be
    # await-free: an await there can be cancelled, and the permit is then gone.
    for st in body[last_acquire + 1:guard]:
        for sub in ast.walk(st):
            assert not isinstance(sub, (ast.Await, ast.AsyncWith, ast.AsyncFor)), (
                "a suspension point sits between sem.acquire() and the try that "
                "releases it - cancellation there leaks the node's only permit")


def test_the_gate_acquisition_is_inside_the_releasing_try():
    fn = _slot_fn()
    guard = next(st for st in fn.body
                 if isinstance(st, ast.Try)
                 and any(_mentions(f, "sem.release") for f in st.finalbody))
    assert any(_mentions(st, "_gate.acquire") for st in guard.body), (
        "the gate acquisition must be inside the try that releases the permit - "
        "it awaits Redis, so a cancellation in it must not skip sem.release()")


try:
    from Vera.vera import capability_orchestration as CO
except Exception:                                      # pragma: no cover
    CO = None


@pytest.mark.skipif(CO is None, reason="app module not importable here")
def test_this_module_actually_imported_the_app():
    """A skipped behavioural test proves nothing - assert we really have it."""
    assert CO is not None and hasattr(CO, "_ollama_slot")


@pytest.mark.skipif(CO is None, reason="app module not importable here")
def test_cancellation_inside_the_gate_hands_the_permit_back():
    iid = "unit-test-permit-node"
    sem = CO._ollama_sem(iid)
    assert sem._value == 1, "fixture node started out already held"

    saved = (CO._GATE_ON, CO.COORD_REDIS, CO._gate.capacity_for, CO._gate.acquire,
             CO._GATE_BROKER_CONFIGURED, CO._GATE_BROKER)

    async def scenario():
        entered = asyncio.Event()

        async def never_returns(*a, **k):
            entered.set()
            await asyncio.sleep(3600)

        CO._GATE_ON = True
        CO._GATE_BROKER_CONFIGURED = False
        CO._GATE_BROKER = None
        CO.COORD_REDIS = object()          # non-None: reach the acquire
        CO._gate.capacity_for = lambda has_gpu: 1
        CO._gate.acquire = never_returns

        async def body():
            async with CO._ollama_slot(iid):
                pass                        # never reached: we cancel first

        task = asyncio.create_task(body())
        await asyncio.wait_for(entered.wait(), timeout=10)
        # Proves the permit really was taken, so the assert below is meaningful.
        assert sem._value == 0, "permit was not held inside the gate block"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(scenario())
    finally:
        CO._GATE_ON, CO.COORD_REDIS = saved[0], saved[1]
        CO._gate.capacity_for, CO._gate.acquire = saved[2], saved[3]
        CO._GATE_BROKER_CONFIGURED, CO._GATE_BROKER = saved[4], saved[5]

    assert sem._value == 1, (
        "a cancelled generation leaked the node's only permit - every later "
        "request on that node would block forever on acquire")
