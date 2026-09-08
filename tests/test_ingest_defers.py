"""The transcript ingest itself must defer and yield - not just the queue.

The queue being correct is not the fix; the fix is the ingest going THROUGH it.
`_scheduled_ingest_all` previously called `cap_claude_sessions_ingest_all`
unconditionally every 300 seconds, which is how a backfill ran straight through
two censuses and cost census 44 three of its first four goals.

These exercise the real wired functions with a fake busy signal, so they fail
if someone unhooks the queue - which is the regression that matters.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CS = pytest.importorskip("vera.ide.ide_claude_sessions_capabilities",
                         reason="app module not importable here")
from vera import background_work as BG                      # noqa: E402
from vera import idle_queue as IQ                           # noqa: E402
from vera import idle_queue_service as SVC                  # noqa: E402

pytestmark = pytest.mark.critical


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_queue(monkeypatch):
    """A clock with no history, so nothing is inherited between tests."""
    q = BG.BackgroundQueue(min_quiet_s=600)
    q.register(CS._JOB, 300, BG.P_BULK)
    monkeypatch.setattr(CS, "_QUEUE", q)
    return q


@pytest.fixture
def idle_store(monkeypatch):
    """In-memory stand-in for the Redis hash the runner reads and writes."""
    rows = {}

    async def load():
        return [dict(v) for v in rows.values()]

    async def save(job):
        rows[job["id"]] = dict(job)

    async def drop(job_id):
        rows.pop(str(job_id), None)

    # Patch the module object the code under test actually holds. CS imported
    # `Vera.vera.idle_queue_service`; `SVC` here is `vera.idle_queue_service` -
    # Python treats those as two unrelated modules, so patching one leaves the
    # other talking to real Redis. (The handler registry and running slot are
    # shared deliberately via sys.modules; these three functions are not, and
    # do not need to be - in production both spellings reach the same Redis.)
    for mod in {SVC, CS._svc}:
        monkeypatch.setattr(mod, "load_jobs", load)
        monkeypatch.setattr(mod, "save_job", save)
        monkeypatch.setattr(mod, "drop_job", drop)
    SVC._RUNNING.clear()
    return rows


def _witnessed_quiet(q, seconds=700.0, step=60.0):
    """Simulate a system OBSERVED idle for `seconds`.

    Deliberately not `last_busy = 0.0` - that was the prod bug (epoch-zero
    reads as decades of quiet), and using it as a fixture is what let the bug
    sit behind four green tests. Real quiet has to be watched tick by tick,
    with no gap longer than stale_s.
    """
    now = time.time()
    t = now - seconds
    while t < now:
        q.observe(t, "")
        t += step
    q.observe(now, "")
    return now


def _never_called(**kw):
    raise AssertionError("the ingest ran while the system was busy")


async def _empty():
    return []


# ── it must not start while busy ────────────────────────────────────────────
def test_the_ingest_does_not_run_during_a_census(monkeypatch, fresh_queue, idle_store):
    async def busy():
        return "a census is running"

    monkeypatch.setattr(CS, "_system_is_busy", busy)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _never_called)
    run(CS._scheduled_ingest_all())
    assert fresh_queue.jobs[CS._JOB]["defers"] == 1
    assert "census" in fresh_queue.jobs[CS._JOB]["last_defer"]


def test_the_ingest_does_not_run_in_a_gap_between_goals(monkeypatch, fresh_queue, idle_store):
    """The bug the whole design turns on: 30-60s of quiet is not quiet."""
    async def free():
        return ""

    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _never_called)
    fresh_queue.observe(time.time() - 30, "an agent loop is running")
    run(CS._scheduled_ingest_all())
    assert fresh_queue.jobs[CS._JOB]["defers"] == 1
    assert "quiet" in fresh_queue.jobs[CS._JOB]["last_defer"]


# ── it must run when genuinely quiet ────────────────────────────────────────
# The tick no longer runs the ingest itself: it queues it and drains the queue.
# That indirection is the point - a queued job can be STOPPED, an inline call
# cannot.

def test_the_tick_queues_the_backfill_and_runs_it_when_quiet(
        monkeypatch, fresh_queue, idle_store):
    calls = {"n": 0}

    async def free():
        return ""

    async def fake_ingest(instance_id="", should_continue=None, **kw):
        calls["n"] += 1
        return {"ok": True, "files_scanned": 3, "files_updated": 3}

    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", fake_ingest)
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())

    async def go():
        _witnessed_quiet(fresh_queue)
        await CS._scheduled_ingest_all()
        task = SVC._RUNNING.get("task")
        if task:
            await task

    run(go())
    assert calls["n"] == 1, "the queued backfill never ran"
    assert idle_store == {}, "a finished job was left on the queue"


def test_the_tick_does_not_queue_a_duplicate_each_interval(
        monkeypatch, fresh_queue, idle_store):
    """Producers are on a timer. An hour of a busy box must not build a herd
    that all lands the moment it goes quiet."""
    async def busy():
        return "a census is running"

    monkeypatch.setattr(CS, "_system_is_busy", busy)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _never_called)

    async def go():
        for _ in range(5):
            await CS._scheduled_ingest_all()

    run(go())
    assert len(idle_store) == 1, \
        "queued %d copies of the same backfill" % len(idle_store)


def test_the_queued_backfill_is_given_a_should_continue_callback(
        monkeypatch, fresh_queue, idle_store):
    """Quiet at the start does not mean quiet throughout."""
    seen = {}

    async def free():
        return ""

    async def fake_ingest(instance_id="", should_continue=None, **kw):
        seen["cb"] = should_continue
        return {"ok": True}

    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", fake_ingest)
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())

    async def go():
        _witnessed_quiet(fresh_queue)
        await CS._scheduled_ingest_all()
        task = SVC._RUNNING.get("task")
        if task:
            await task

    run(go())
    assert callable(seen.get("cb")), "the ingest was given no way to yield"


def test_a_failed_backfill_frees_the_queue(monkeypatch, fresh_queue, idle_store):
    """A crash must not leave the runner permanently 'running', or no
    background work ever happens again."""
    async def free():
        return ""

    async def boom(instance_id="", should_continue=None, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", boom)
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())

    async def go():
        _witnessed_quiet(fresh_queue)
        await CS._scheduled_ingest_all()
        task = SVC._RUNNING.get("task")
        if task:
            await task

    run(go())
    assert SVC._RUNNING == {}, "the runner slot was never released"
    assert idle_store == {}


# ── the yield is HONOURED, not merely offered ───────────────────────────────
def test_a_running_ingest_stops_when_the_box_gets_busy(monkeypatch):
    """The callback existing proves nothing; it has to be obeyed.

    Ten files, and the box goes busy after the third. The pass must stop there
    - state is checkpointed per file, so the next pass resumes from the same
    offsets and nothing is lost.
    """
    files = [{"rel": "home::p/%d.jsonl" % i, "size": 100} for i in range(10)]
    done = {"n": 0}

    async def fake_scan(instance_id=""):
        return {"files": files}

    async def fake_ingest_file(instance_id, rel, state):
        done["n"] += 1
        return 1

    async def busy_after_three():
        return "a census is running" if done["n"] >= 3 else ""

    monkeypatch.setattr(CS, "cap_claude_sessions_scan", fake_scan)
    monkeypatch.setattr(CS, "_ingest_file", fake_ingest_file)
    monkeypatch.setattr(CS, "_load_state", lambda: {})
    monkeypatch.setattr(CS, "_save_state", lambda s: None)

    res = run(CS.cap_claude_sessions_ingest_all(
        instance_id="", should_continue=busy_after_three))

    assert done["n"] == 3, \
        "kept going after the box got busy (did %d of 10)" % done["n"]
    assert res.get("yielded"), "stopped but did not report why"
    assert "census" in res["yielded"]


def test_an_ingest_with_no_callback_still_completes(monkeypatch):
    """A manual call (no queue) must not be crippled by the yield machinery."""
    files = [{"rel": "home::p/%d.jsonl" % i, "size": 100} for i in range(4)]
    done = {"n": 0}

    async def fake_scan(instance_id=""):
        return {"files": files}

    async def fake_ingest_file(instance_id, rel, state):
        done["n"] += 1
        return 1

    monkeypatch.setattr(CS, "cap_claude_sessions_scan", fake_scan)
    monkeypatch.setattr(CS, "_ingest_file", fake_ingest_file)
    monkeypatch.setattr(CS, "_load_state", lambda: {})
    monkeypatch.setattr(CS, "_save_state", lambda s: None)
    res = run(CS.cap_claude_sessions_ingest_all(instance_id=""))
    assert done["n"] == 4 and not res.get("yielded")


# ── it is wired in as bulk, and as a pre-emptible kind ──────────────────────
def test_the_ingest_is_registered_as_BULK_priority():
    """It must always yield to narration, dreams and anything interactive."""
    assert CS._QUEUE is None or CS._QUEUE.jobs[CS._JOB]["priority"] == BG.P_BULK


def test_the_backfill_is_a_preemptible_kind():
    """Embedding runs on the CPU nodes with no loop and no GPU gate, so
    activity really is somebody else - it can be stopped mid-flight."""
    assert IQ.is_preemptible(IQ.KIND_EMBED_SESSIONS)


def test_a_handler_is_registered_for_the_backfill():
    """Without this the runner drops the job as 'no handler' and the backfill
    silently never happens."""
    assert SVC.has_handler(IQ.KIND_EMBED_SESSIONS)
