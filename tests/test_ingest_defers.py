"""The transcript IMPORT runs now; the EMBEDDING defers and yields.

The original contract was "the ingest goes through the queue", because an
ingest that embedded each turn inline ran straight through two censuses and
cost census 44 three of its first four goals. What was dangerous was the
embedding, not the import: with embedding deferred, an import is file reads
and row writes, and the rows are what the UI shows. So since 2026-09-11:

  - the tick runs the import DIRECTLY, always with defer_embedding=True, as a
    single in-flight task (never a herd);
  - the fabric queues one embed.fabric backfill for the rows it stored without
    vectors, and THAT waits for the idle box;
  - the embed.sessions handler still exists and still yields, for an explicit
    embedding run.

These exercise the real wired functions with a fake busy signal, so they fail
if someone makes the tick embed inline again - the regression that matters.
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
        return True                     # save_job's verdict: it really stored

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


async def _empty():
    return []


async def _tick_and_wait():
    """Run the tick, then await the import task it may have kicked off."""
    await CS._scheduled_ingest_all()
    t = getattr(CS, "_IMPORT_TASK", None)
    if t is not None and not t.done():
        await t


def _capturing_ingest(calls):
    async def fake(instance_id="", should_continue=None, **kw):
        calls.append({"instance_id": instance_id, "cb": should_continue, **kw})
        return {"ok": True, "files_scanned": 3, "files_updated": 3}
    return fake


# ── it must not start while busy ────────────────────────────────────────────
def test_during_a_census_the_import_runs_but_never_embeds(monkeypatch, fresh_queue, idle_store):
    """The rows land (visible), the vectors wait. An inline embed here is the
    exact regression that cost census 44 three goals."""
    calls = []

    async def busy():
        return "a census is running"

    monkeypatch.setattr(CS, "_system_is_busy", busy)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _capturing_ingest(calls))
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())
    monkeypatch.setattr(CS, "_IMPORT_TASK", None)
    run(_tick_and_wait())
    assert calls, "the import did not run - a new session would stay invisible until the box was idle"
    assert all(c.get("defer_embedding") is True for c in calls), calls
    assert fresh_queue.jobs[CS._JOB]["defers"] == 1
    assert "census" in fresh_queue.jobs[CS._JOB]["last_defer"]


def test_the_ingest_does_not_run_in_a_gap_between_goals(monkeypatch, fresh_queue, idle_store):
    """The bug the whole design turns on: 30-60s of quiet is not quiet."""
    async def free():
        return ""

    calls = []
    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _capturing_ingest(calls))
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())
    monkeypatch.setattr(CS, "_IMPORT_TASK", None)
    fresh_queue.observe(time.time() - 30, "an agent loop is running")
    run(_tick_and_wait())
    assert all(c.get("defer_embedding") is True for c in calls), calls
    assert fresh_queue.jobs[CS._JOB]["defers"] == 1
    assert "quiet" in fresh_queue.jobs[CS._JOB]["last_defer"]


# ── it must run when genuinely quiet ────────────────────────────────────────
# The tick no longer runs the ingest itself: it queues it and drains the queue.
# That indirection is the point - a queued job can be STOPPED, an inline call
# cannot.

def test_the_tick_imports_directly_and_queues_no_backfill_of_its_own(
        monkeypatch, fresh_queue, idle_store):
    """Embedding flows through the FABRIC's own embed.fabric backfill (queued
    inside ingest_dataset, out of scope here). The tick queues nothing."""
    calls = []

    async def free():
        return ""

    monkeypatch.setattr(CS, "_system_is_busy", free)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", _capturing_ingest(calls))
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())
    monkeypatch.setattr(CS, "_IMPORT_TASK", None)

    async def go():
        _witnessed_quiet(fresh_queue)
        await _tick_and_wait()

    run(go())
    assert len(calls) == 1, "the import ran %d times for one tick" % len(calls)
    assert calls[0].get("defer_embedding") is True
    assert idle_store == {}, "the tick queued a job of its own: %s" % idle_store


def test_five_ticks_start_one_import_not_five(monkeypatch, fresh_queue, idle_store):
    """Producers are on a timer. A slow import must not be joined by four
    more of itself, and nothing may be queued behind it."""
    calls = []
    gate = asyncio.Event()

    async def busy():
        return "a census is running"

    async def slow_ingest(instance_id="", should_continue=None, **kw):
        calls.append(kw)
        await gate.wait()
        return {"ok": True}

    monkeypatch.setattr(CS, "_system_is_busy", busy)
    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", slow_ingest)
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())
    monkeypatch.setattr(CS, "_IMPORT_TASK", None)

    async def go():
        for _ in range(5):
            await CS._scheduled_ingest_all()
            await asyncio.sleep(0)
        gate.set()
        t = getattr(CS, "_IMPORT_TASK", None)
        if t is not None:
            await t

    run(go())
    assert len(calls) == 1, "started %d imports for five ticks" % len(calls)
    assert idle_store == {}, "queued %d job(s)" % len(idle_store)


def test_an_explicit_embedding_run_is_given_a_should_continue_callback(monkeypatch):
    """The embed.sessions handler is what an explicit (non-deferred) run goes
    through, and quiet at the start does not mean quiet throughout."""
    seen = {}

    async def fake_ingest(instance_id="", should_continue=None, **kw):
        seen["cb"] = should_continue
        seen["kw"] = kw
        return {"ok": True}

    async def free():
        return ""

    monkeypatch.setattr(CS, "cap_claude_sessions_ingest_all", fake_ingest)
    monkeypatch.setattr(CS, "_load_instances", lambda: _empty())
    run(CS._ingest_job({"id": "embed.sessions:x", "kind": IQ.KIND_EMBED_SESSIONS}, free))
    assert callable(seen.get("cb")), "the ingest was given no way to yield"
    assert not seen["kw"].get("defer_embedding"), "an explicit embedding run must embed"


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

    async def fake_ingest_file(instance_id, rel, state, **kw):
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

    async def fake_ingest_file(instance_id, rel, state, **kw):
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
