"""A run is dead only when it is dead, not merely quiet.

Census author-then-edit (2026-08-29) executed all 3 of its planned steps, emitted
zero warnings, went quiet for one long final generation, and was reported
"interrupted" at 874s -- then showed "running" again ~50 minutes later. The
harness had already recorded a failure and moved on, while the run kept holding
the single GPU slot, so the next goal sat waiting on a gate held by work believed
abandoned.

Two causes, both fixed here:

  * the fallback staleness window (600s) was SHORTER than one legitimate
    generation (OLLAMA_GEN_TIMEOUT, 900s);
  * the live-task short-circuit could never fire for a cap-invoked run, because
    only the SSE /stream wrapper registered its task. Every loop started via
    /mcp/call was invisible -- which is also why every cancel answered "no live
    runner task in this process" while the run carried on.
"""
import asyncio
import os
import time

import pytest

DAG = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                   "dag_workshop_capabilities.py")


class _AncientMarker:
    """Redis stand-in whose activity marker is far outside any stale window."""
    async def zscore(self, key, member):
        return time.time() - 100_000


try:
    from Vera.vera.dag import dag_workshop_capabilities as W
except Exception:                                      # pragma: no cover
    W = None

pytestmark = pytest.mark.skipif(W is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    assert W is not None and hasattr(W, "_loop_run_is_stale")
    assert hasattr(W, "_register_loop_task"), "the runner registry helper is missing"


def test_the_stale_window_outlasts_one_generation():
    """The rule _GATE_MAX_HOLD_S already follows; this is where it was missed."""
    assert W._LOOP_STALE_SECS > W._LOOP_GEN_TIMEOUT_S, (
        "a run mid-generation is declared dead: stale window %s <= generation "
        "timeout %s" % (W._LOOP_STALE_SECS, W._LOOP_GEN_TIMEOUT_S))


def test_a_live_registered_run_is_never_called_stale():
    """The exact false-interrupted case: quiet for ages, but demonstrably alive."""
    sid = "unit-live-run"

    async def scenario():
        W._register_loop_task(sid)
        assert W._AGENT_LOOP_TASKS.get(sid) is asyncio.current_task(), \
            "a cap-invoked run did not register itself"
        return await W._loop_run_is_stale(_AncientMarker(), sid, {"status": "running"})

    try:
        assert asyncio.run(scenario()) is False
    finally:
        W._AGENT_LOOP_TASKS.pop(sid, None)


def test_a_run_with_no_live_task_and_no_recent_activity_is_still_stale():
    """The guard must not swing the other way and hide genuinely dead runs."""
    async def scenario():
        return await W._loop_run_is_stale(
            _AncientMarker(), "unit-no-such-run", {"status": "running"})

    assert asyncio.run(scenario()) is True


def test_the_registration_is_dropped_when_the_run_finishes():
    sid = "unit-cleanup"

    async def scenario():
        async def child():
            W._register_loop_task(sid)
            assert sid in W._AGENT_LOOP_TASKS
        await asyncio.create_task(child())
        await asyncio.sleep(0)          # let the done-callback run
        return W._AGENT_LOOP_TASKS.get(sid)

    try:
        assert asyncio.run(scenario()) is None, "the registry leaks finished runs"
    finally:
        W._AGENT_LOOP_TASKS.pop(sid, None)


def test_registration_never_displaces_an_existing_runner():
    """The SSE wrapper is the real owner when both paths touch one session."""
    sid = "unit-no-displace"

    async def scenario():
        sentinel = asyncio.create_task(asyncio.sleep(3600))
        W._AGENT_LOOP_TASKS[sid] = sentinel
        W._register_loop_task(sid)
        kept = W._AGENT_LOOP_TASKS.get(sid) is sentinel
        sentinel.cancel()
        return kept

    try:
        assert asyncio.run(scenario()) is True
    finally:
        W._AGENT_LOOP_TASKS.pop(sid, None)


def test_the_underlying_status_is_preserved_wherever_it_is_derived():
    """`status` stays derived for the client; `status_raw` keeps the truth."""
    src = open(DAG, encoding="utf-8").read()
    overwrites = src.count('run["status"] = "interrupted"')
    assert overwrites >= 1
    assert src.count('run["status_raw"]') == overwrites, (
        "a derivation site overwrites status without preserving the raw value")
