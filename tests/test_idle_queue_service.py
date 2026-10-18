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
import time

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
        return True                     # save_job's verdict: it really stored

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


# â”€â”€ a queue that cannot store must SAY so â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# background.enqueue returned ok:True while storing nothing, because the write
# failed into a debug log. Found by enqueueing on a live instance and finding
# no row. "Queued" that did not queue is worse than a refusal: the work is
# silently never done and the panel shows an empty, healthy-looking queue.

def test_submit_reports_failure_when_the_store_is_down(monkeypatch, store):
    async def dead_save(job):
        return False

    monkeypatch.setattr(SVC, "save_job", dead_save)

    async def go():
        return await SVC.submit(IQ.KIND_DREAM, "a dream")

    res = run(go())
    assert res["queued"] is False
    assert "unavailable" in res["reason"]


def test_save_job_is_false_with_no_redis(monkeypatch):
    monkeypatch.setattr(SVC, "_redis", lambda: None)

    async def go():
        return await SVC.save_job({"id": "x"})

    assert run(go()) is False


def test_load_jobs_is_empty_not_an_error_with_no_redis(monkeypatch):
    """Reading must degrade quietly - the panel asks every refresh."""
    monkeypatch.setattr(SVC, "_redis", lambda: None)

    async def go():
        return await SVC.load_jobs()

    assert run(go()) == []


# ── reserved windows (intel's night in the Loop Lab schedule, 17 Oct 2026) ──

def test_a_reserved_window_starts_its_kinds_despite_the_gate(store):
    """Six days of censuses never gave the queue 600 s of quiet. A reservation sets the gate aside for the
    reserved kinds only; everything else still waits."""
    ran = []

    async def handler(job, should_continue):
        ran.append(job["kind"])
        return {"ok": True}

    SVC.register_handler(IQ.KIND_DREAM, handler)
    SVC.register_handler(IQ.KIND_NARRATOR, handler)

    async def go():
        await SVC.submit(IQ.KIND_NARRATOR, "a narration")      # not reserved: must not start
        await SVC.submit(IQ.KIND_DREAM, "a dream")
        SVC.reserve((IQ.KIND_DREAM,), until=100.0 + 600, by="test")
        res = await SVC.drain_once("a census is running", _busy, now=100.0)
        assert res["action"] == "started" and res["kind"] == IQ.KIND_DREAM, res
        await SVC._RUNNING["task"]
        res2 = await SVC.drain_once("a census is running", _busy, now=101.0)
        assert res2["action"] == "idle" and "reserved" in res2["reason"], res2
        SVC.release_reservation("test")
        res3 = await SVC.drain_once("a census is running", _busy, now=102.0)
        assert res3["action"] == "idle" and res3["reason"] == "a census is running"

    run(go())
    assert ran == [IQ.KIND_DREAM]
    assert [j["kind"] for j in store.values()] == [IQ.KIND_NARRATOR]


def test_a_reserved_job_is_not_preempted_and_the_reservation_expires(store):
    seen = []

    async def handler(job, should_continue):
        seen.append(await should_continue())        # '' while reserved, even though the box says busy
        return {"ok": True}

    SVC.register_handler(IQ.KIND_EMBED_FABRIC, handler)

    async def go():
        await SVC.submit(IQ.KIND_EMBED_FABRIC, "a backfill")
        t0 = time.time()                       # the handler's should_continue reads the real clock
        SVC.reserve((IQ.KIND_EMBED_FABRIC,), until=t0 + 600, by="test")
        res = await SVC.drain_once("an agent loop is running", _busy, now=t0)
        assert res["action"] == "started"
        busy = await SVC.drain_once("an agent loop is running", _busy, now=t0 + 1)
        assert busy["action"] == "busy" and "reserved" in busy["note"], busy
        await SVC._RUNNING["task"]
        assert SVC.reservation(now=t0 + 599) is not None
        assert SVC.reservation(now=t0 + 600) is None              # expired: cleared on read

    run(go())
    assert seen == [""]
    SVC.release_reservation()


def test_only_the_holder_releases_its_reservation():
    SVC.reserve(("x",), until=10.0, by="a")
    SVC.release_reservation("b")
    assert SVC.reservation(now=0.0) is not None
    SVC.release_reservation("a")
    assert SVC.reservation(now=0.0) is None



def test_a_tick_drops_duplicate_copies_of_one_key(store):
    """Copies that got past submit (a failed read, two timers racing) are collapsed by the runner."""
    for i, t in enumerate((30, 10, 20)):
        store["j%d" % i] = IQ.make_job("j%d" % i, IQ.KIND_NARRATOR, "quick take", enqueued_at=t)
        store["j%d" % i]["dedupe_key"] = "narrator:quick"

    async def go():
        return await SVC.drain_once("busy", _busy)          # busy: nothing starts, the collapse still runs

    run(go())
    assert sorted(store) == ["j1"]                          # the oldest copy keeps its place


def test_submit_does_not_enqueue_when_it_cannot_read_the_queue(monkeypatch, store):
    """An unreadable store looked like an empty queue, so every hourly producer added another copy."""
    store["old"] = IQ.make_job("old", IQ.KIND_NARRATOR, "quick take", enqueued_at=1)
    store["old"]["dedupe_key"] = "narrator:quick"

    async def broken_load():
        SVC._LOAD_OK = False
        return []

    monkeypatch.setattr(SVC, "load_jobs", broken_load)

    async def go():
        return await SVC.submit(IQ.KIND_NARRATOR, "quick take", dedupe_key="narrator:quick")

    res = run(go())
    SVC._LOAD_OK = True
    assert res["queued"] is False and "could not read" in res["reason"]
    assert sorted(store) == ["old"]
