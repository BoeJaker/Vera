"""The runner: does the queue actually start things, and let go of the box?

`test_idle_queue.py` covers the pure decisions. This covers the half that has
consequences - dispatch, and pre-emption of work already in flight.

Prod shipped a queue whose `next_job`/`preempt` were called from nowhere: rows
went into Redis and nothing ever read them. So the first thing asserted here is
simply that a queued job runs at all.

Redis is replaced with a dict. Pre-emption is the whole point and it must be
testable without a live Ollama node.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import idle_queue as IQ                          # noqa: E402
from vera import idle_queue_service as SVC                 # noqa: E402

pytestmark = pytest.mark.critical


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for the Redis hash."""
    rows = {}

    async def load():
        return [dict(v) for v in rows.values()]

    async def save(job):
        rows[job["id"]] = dict(job)

    async def drop(job_id):
        rows.pop(str(job_id), None)

    monkeypatch.setattr(SVC, "load_jobs", load)
    monkeypatch.setattr(SVC, "save_job", save)
    monkeypatch.setattr(SVC, "drop_job", drop)
    monkeypatch.setattr(SVC, "_HANDLERS", {})
    SVC._RUNNING.clear()
    return rows


async def _free():
    return ""


async def _busy():
    return "an agent loop is running"


# ── it runs things ──────────────────────────────────────────────────────────
def test_a_queued_job_actually_runs(store):
    """The regression that shipped: rows in, nothing out."""
    ran = {"n": 0}

    async def handler(job, should_continue):
        ran["n"] += 1
        return {"ok": True}

    SVC.register_handler(IQ.KIND_DREAM, handler)

    async def go():
        await SVC.submit(IQ.KIND_DREAM, "a dream")
        res = await SVC.drain_once("", _free, now=100.0)
        assert res["action"] == "started", res
        await SVC._RUNNING["task"]
        return res

    run(go())
    assert ran["n"] == 1
    assert store == {}, "a finished job was left on the queue"


def test_nothing_starts_while_the_gate_is_blocked(store):
    async def handler(job, should_continue):
        raise AssertionError("ran during active use")

    SVC.register_handler(IQ.KIND_NARRATOR, handler)

    async def go():
        await SVC.submit(IQ.KIND_NARRATOR, "a narration")
        return await SVC.drain_once("a census is running", _busy, now=100.0)

    res = run(go())
    assert res["action"] == "idle"
    assert res["reason"] == "a census is running"
    assert res["depth"] == 1, "the job should still be waiting"


def test_only_one_job_runs_at_a_time(store):
    started = {"n": 0}

    async def slow(job, should_continue):
        started["n"] += 1
        await asyncio.sleep(5)

    SVC.register_handler(IQ.KIND_DREAM, slow)

    async def go():
        # Same kind on purpose: a narrator job would sort AHEAD of the dream
        # (priority 10 vs 20) and, with no handler registered for it, would be
        # dropped rather than run - which is a different test.
        await SVC.submit(IQ.KIND_DREAM, "one", dedupe_key="d1")
        await SVC.submit(IQ.KIND_DREAM, "two", dedupe_key="d2")
        await SVC.drain_once("", _free, now=100.0)
        await asyncio.sleep(0.01)          # let the created task actually begin
        second = await SVC.drain_once("", _free, now=101.0)
        SVC._RUNNING["task"].cancel()
        return second

    res = run(go())
    assert res["action"] == "busy"
    assert started["n"] == 1


# ── it lets go of the box ───────────────────────────────────────────────────
def test_activity_asks_the_running_job_to_yield(store):
    """First contact is a request, not a kill - the handler checkpoints."""
    saw = {"stop": None}

    async def polite(job, should_continue):
        for _ in range(50):
            saw["stop"] = await should_continue()
            if saw["stop"]:
                return {"yielded": saw["stop"]}
            await asyncio.sleep(0.01)
        return {"ok": True}

    SVC.register_handler(IQ.KIND_EMBED_SESSIONS, polite)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_SESSIONS, "backfill")
        await SVC.drain_once("", _free, now=100.0)
        res = await SVC.drain_once("a census is running", _busy, now=101.0)
        await asyncio.sleep(0.05)
        return res

    res = run(go())
    assert res["action"] == "preempting"
    assert saw["stop"], "the handler was never told to stop"


def test_a_job_that_ignores_the_ask_is_cancelled_and_requeued(store):
    """A handler with no yield point must not hold the node indefinitely."""
    async def stubborn(job, should_continue):
        await asyncio.sleep(60)

    SVC.register_handler(IQ.KIND_EMBED_SOURCES, stubborn)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_SOURCES, "a long source embed")
        await SVC.drain_once("", _free, now=100.0)
        await SVC.drain_once("busy", _busy, now=101.0)          # asks politely
        # past the grace window: it gets cancelled
        await SVC.drain_once("busy", _busy, now=101.0 + SVC.PREEMPT_GRACE_S + 1)
        await asyncio.sleep(0.02)

    run(go())
    left = list(store.values())
    assert len(left) == 1
    assert left[0]["state"] == IQ.WAITING, "a pre-empted job must go back on the queue"
    assert left[0]["preempts"] == 1
    assert "pre-empted" in left[0]["last_note"]


def test_the_grace_window_is_respected(store):
    """Cancelling instantly would throw away a checkpoint it was about to make."""
    async def slow(job, should_continue):
        await asyncio.sleep(60)

    SVC.register_handler(IQ.KIND_EMBED_SOURCES, slow)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_SOURCES, "d")
        await SVC.drain_once("", _free, now=100.0)
        await SVC.drain_once("busy", _busy, now=101.0)
        await SVC.drain_once("busy", _busy, now=101.0 + SVC.PREEMPT_GRACE_S - 1)
        alive = not SVC._RUNNING["task"].done()
        SVC._RUNNING["task"].cancel()
        return alive

    assert run(go()), "cancelled before its grace window had elapsed"


# ── housekeeping ────────────────────────────────────────────────────────────
def test_duplicate_work_is_not_queued_twice(store):
    """Producers are on timers; an hour of a busy box must not build a herd."""
    async def go():
        a = await SVC.submit(IQ.KIND_NARRATOR, "quick take",
                             dedupe_key="narrator:quick")
        b = await SVC.submit(IQ.KIND_NARRATOR, "quick take",
                             dedupe_key="narrator:quick")
        return a, b

    a, b = run(go())
    assert a["queued"] is True
    assert b["queued"] is False and b["reason"] == "already queued"
    assert len(store) == 1


def test_a_job_with_no_handler_is_dropped_not_stuck(store):
    async def go():
        await SVC.submit("nonsense.kind", "orphan")
        return await SVC.drain_once("", _free, now=100.0)

    res = run(go())
    assert res["action"] == "dropped"
    assert store == {}, "an unrunnable job would block the queue forever"


def test_a_failing_handler_frees_the_slot(store):
    async def boom(job, should_continue):
        raise RuntimeError("nope")

    SVC.register_handler(IQ.KIND_DREAM, boom)

    async def go():
        await SVC.submit(IQ.KIND_DREAM, "d")
        await SVC.drain_once("", _free, now=100.0)
        await SVC._RUNNING["task"]

    run(go())
    assert SVC._RUNNING == {}, "a crash left the queue permanently running"
    assert store == {}


def test_a_handler_that_yields_itself_is_requeued_not_dropped(store):
    """It stopped early, so the rest of its work still needs doing."""
    async def yielder(job, should_continue):
        return {"yielded": "pre-empted by the idle queue"}

    SVC.register_handler(IQ.KIND_EMBED_SOURCES, yielder)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_SOURCES, "sources")
        await SVC.drain_once("", _free, now=100.0)
        await SVC._RUNNING["task"]

    run(go())
    left = list(store.values())
    assert len(left) == 1 and left[0]["state"] == IQ.WAITING


# â”€â”€ pre-emption must not cancel the queue's own work â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A dream runs an agent loop and holds the GPU gate. Every "the box is busy"
# signal is therefore true BECAUSE the queue started it. Pre-empting on that
# livelocks: start, self-detect, cancel, requeue, repeat.

def test_a_dream_is_not_preempted_by_its_own_activity(store):
    async def dreaming(job, should_continue):
        await asyncio.sleep(60)

    SVC.register_handler(IQ.KIND_DREAM, dreaming)

    async def go():
        await SVC.submit(IQ.KIND_DREAM, "a dream")
        await SVC.drain_once("", _free, now=100.0)
        # The dream is now running, so of course the box reads as busy.
        r1 = await SVC.drain_once("the GPU gate is held by dream", _busy, now=101.0)
        r2 = await SVC.drain_once("the GPU gate is held by dream", _busy,
                                  now=101.0 + SVC.PREEMPT_GRACE_S + 5)
        alive = not SVC._RUNNING["task"].done()
        SVC._RUNNING["task"].cancel()
        return r1, r2, alive

    r1, r2, alive = run(go())
    assert r1["action"] == "busy" and "not pre-emptible" in r1["note"]
    assert r2["action"] == "busy", "cancelled itself once past the grace window"
    assert alive, "the queue cancelled its own dream"
    assert list(store.values())[0]["state"] == IQ.RUNNING


def test_embedding_IS_preempted(store):
    """The case the whole thing exists for. CPU nodes, no loop, no gate - so
    activity really is somebody else."""
    async def embedding(job, should_continue):
        await asyncio.sleep(60)

    SVC.register_handler(IQ.KIND_EMBED_SESSIONS, embedding)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_SESSIONS, "backfill")
        await SVC.drain_once("", _free, now=100.0)
        await SVC.drain_once("a census is running", _busy, now=101.0)
        await SVC.drain_once("a census is running", _busy,
                             now=101.0 + SVC.PREEMPT_GRACE_S + 1)
        await asyncio.sleep(0.02)

    run(go())
    left = list(store.values())
    assert left[0]["state"] == IQ.WAITING, "the backfill kept the CPU node"
    assert left[0]["preempts"] == 1
