"""A census row without a time of its own takes its archive's END time, and
old rows stay out of the run list.

Found live 2026-09-10 after the first backfill (ingest_census.py --all): 472
rows from every archive since August went into the 400-record run list, all
stamped with the ingest time (rows before 2026-09-10 carry no time), and the
12 real run records that were in the list fell off the end. The index had a
twin fault: its fallback was the archive's mtime, which the backfill tools
had just rewritten, so a task's pre-today history sorted by session id.

Pinned here: the archive's `.log` (written as the run goes, never rewritten)
is the time source; the cap takes `ts_fallback` and keeps run records only
inside the detail window; census results with equal times order by run
number.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import task_history_core as th  # noqa: E402

pytestmark = pytest.mark.critical


def test_equal_times_order_by_census_run_number():
    rows = [{"task_id": "t", "run_id": "zzz", "ts": "2026-09-10T15:29:45Z", "driver": {"kind": "census", "id": "run45"}, "status": "done", "ok": True},
            {"task_id": "t", "run_id": "aaa", "ts": "2026-09-10T15:29:45Z", "driver": {"kind": "census", "id": "run47"}, "status": "done", "ok": True},
            {"task_id": "t", "run_id": "mmm", "ts": "2026-09-10T15:29:45Z", "driver": {"kind": "census", "id": "run46"}, "status": "done", "ok": True},
            {"task_id": "t", "run_id": "x", "ts": "2026-09-11T00:00:00Z", "driver": {"kind": "suite", "id": "s1"}, "status": "pass", "ok": True}]
    h = th.task_history(rows, "t")
    assert [r["driver"]["id"] for r in h["results"]] == ["s1", "run47", "run46", "run45"], "newest first; ties by run number, not session id"
    ov = th.tasks_overview(rows)
    assert [p["driver"] for p in ov[0]["series"]] == ["run45", "run46", "run47", "s1"]
    assert th.order_key({"ts": "", "driver": {"id": "exec-family-run3"}, "run_id": "r"}) == ("", 3, "r")
    assert th.order_key({"ts": "t", "driver": {"id": "abc"}, "run_id": "r"}) == ("t", 0, "r")


def _app():
    try:
        from Vera.vera.evolve import task_history_capabilities as TH
    except Exception:
        return None
    root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
    return TH if os.path.realpath(getattr(TH, "__file__", "")).startswith(root) else None


def test_the_archives_time_is_its_logs_mtime_not_the_rewritten_jsonls(tmp_path):
    TH = _app()
    if TH is None:
        pytest.skip("app module not importable from THIS checkout here")
    arc = tmp_path / "census.run45.jsonl"
    arc.write_text("{}\n")
    log = tmp_path / "census.run45.log"
    log.write_text("x")
    ended = time.time() - 5 * 86400
    os.utime(log, (ended, ended))
    os.utime(arc, (time.time(), time.time()))           # rewritten today by a backfill tool
    assert TH.archive_time(arc) == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended))
    lone = tmp_path / "census.run46.jsonl"
    lone.write_text("{}\n")
    os.utime(lone, (ended, ended))
    assert TH.archive_time(lone) == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)), "no log: the archive itself"
    assert TH.archive_time(tmp_path / "missing.jsonl") == ""


def test_the_cap_takes_the_archives_time_and_keeps_only_the_window(monkeypatch):
    """No Redis needed: the store calls are stubbed; what is pinned is which
    records reach the store and with which time."""
    TH = _app()
    if TH is None:
        pytest.skip("app module not importable from THIS checkout here")
    from Vera.vera.evolve import evolve_capabilities as EV
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setattr(EV, "_redis", lambda: object())
    stored, suites = [], []

    async def _up_run(ev, compact, detail):
        stored.append(compact)
        return "added"

    async def _up_suite(ev, summary):
        suites.append(summary)
        return "added"

    async def _tasks(ev):
        return []
    monkeypatch.setattr(TH, "_upsert_run", _up_run)
    monkeypatch.setattr(TH, "_upsert_suite", _up_suite)
    monkeypatch.setattr(TH, "_tasks", _tasks)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 86400))
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400))
    rows = [{"id": "a", "session_id": "s-a", "status": "done", "wall_s": 10},                       # no time: takes ts_fallback
            {"id": "b", "session_id": "s-b", "status": "done", "wall_s": 10, "ended_at": recent}]   # its own time
    out = asyncio.run(TH.cap_evolve_result_ingest(template="default", census_run="run40", rows=rows,
                                                  suite=True, ts_fallback=old))
    assert out["ok"] and out["added"] == 1 and out["skipped_old"] == 1 and out["run_ids"] == ["s-b"], out
    assert stored[0]["ts"] == recent
    assert suites and suites[0]["suite_id"] == "run40" and suites[0]["tasks_n"] == 2, "the scoreboard keeps every row"
    assert suites[0]["ts"] == recent, "the run's time is its latest row's"
    stored.clear(); suites.clear()
    out = asyncio.run(TH.cap_evolve_result_ingest(template="default", census_run="run52", rows=rows[:1],
                                                  ts_fallback=recent))
    assert out["added"] == 1 and stored[0]["ts"] == recent, "inside the window: the fallback time is the record's"
    stored.clear()
    out = asyncio.run(TH.cap_evolve_result_ingest(template="default", census_run="run53", rows=rows[:1]))
    assert out["added"] == 1 and stored[0]["ts"][:13] == time.strftime("%Y-%m-%dT%H", time.gmtime()), "no fallback given: now"


# ── an ingested record takes its place by time ───────────────────────────────
def test_insert_position_keeps_the_list_newest_first():
    TH = _app()
    if TH is None:
        pytest.skip("app module not importable from THIS checkout here")
    rows = [{"ts": "2026-09-10T15:00:00Z"}, {"ts": "2026-09-09T12:00:00Z"}, {"ts": "2026-09-08T08:00:00Z"}]
    assert TH.insert_position(rows, "2026-09-11T00:00:00Z", 400) == 0, "newest: the head"
    assert TH.insert_position(rows, "2026-09-09T20:00:00Z", 400) == 1, "between: before the first no-newer entry"
    assert TH.insert_position(rows, "2026-09-09T12:00:00Z", 400) == 1, "equal time: before its equal (later insert reads as newer)"
    assert TH.insert_position(rows, "2026-09-01T00:00:00Z", 400) == 3, "oldest: the tail"
    assert TH.insert_position(rows, "2026-09-01T00:00:00Z", 3) is None, "full list, every entry newer: beyond the window"
    assert TH.insert_position([], "2026-09-01T00:00:00Z", 400) == 0
    assert TH.insert_position([{"ts": ""}], "2026-09-01T00:00:00Z", 400) == 0, "an entry with no time counts as oldest"


def test_the_store_keeps_records_in_time_order_and_drops_the_beyond_window(monkeypatch):
    """Against a real Redis when one is reachable (REDIS_URL, not db 0);
    skipped otherwise - test_result_ingest_boot explains the setup."""
    TH = _app()
    if TH is None:
        pytest.skip("app module not importable from THIS checkout here")
    import json
    import uuid
    from Vera.vera.evolve import evolve_capabilities as EV

    async def _go():
        url = os.getenv("REDIS_URL") or ""
        if not url:
            pytest.skip("no REDIS_URL")
        import redis.asyncio as aioredis
        r = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=3, socket_timeout=5)
        if int(r.connection_pool.connection_kwargs.get("db") or 0) == 0:
            pytest.skip("database 0 is prod's")
        try:
            await r.ping()
        except Exception:
            pytest.skip("Redis unreachable")
        monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
        monkeypatch.setattr(EV, "_redis", lambda: r)
        monkeypatch.setattr(EV, "RUNS_CAP", 4)
        tag = uuid.uuid4().hex[:6]
        key = EV.KEY_RUNS
        saved = await r.lrange(key, 0, -1)
        await r.delete(key)
        try:
            def row(gid, ended):
                return {"id": gid, "session_id": gid + "-" + tag, "status": "done", "wall_s": 1, "ended_at": ended}
            async def ingest(rows, run):
                return await TH.cap_evolve_result_ingest(template="default", census_run=run, rows=rows)
            out = await ingest([row("b", "2026-09-09T00:00:00Z"), row("d", "2026-09-07T00:00:00Z")], "run2")
            assert out["added"] == 2
            out = await ingest([row("a", "2026-09-10T00:00:00Z"), row("c", "2026-09-08T00:00:00Z")], "run3")
            assert out["added"] == 2
            order = [json.loads(x)["label"] for x in await r.lrange(key, 0, -1)]
            assert order == ["a", "b", "c", "d"], order
            # full list (cap 4): a record older than everything is not added
            out = await ingest([row("e", "2026-09-01T00:00:00Z")], "run1")
            assert out["added"] == 0 and out["skipped_old"] == 1 and out["run_ids"] == []
            assert [json.loads(x)["label"] for x in await r.lrange(key, 0, -1)] == ["a", "b", "c", "d"]
            # a newer one goes to the head and the oldest falls off
            out = await ingest([row("f", "2026-09-11T00:00:00Z")], "run4")
            assert out["added"] == 1
            assert [json.loads(x)["label"] for x in await r.lrange(key, 0, -1)] == ["f", "a", "b", "c"]
            # re-posting keeps the place
            out = await ingest([row("b", "2026-09-09T00:00:00Z")], "run2")
            assert out["replaced"] == 1
            assert [json.loads(x)["label"] for x in await r.lrange(key, 0, -1)] == ["f", "a", "b", "c"]
        finally:
            await r.delete(key)
            for x in reversed(saved):
                await r.lpush(key, x)
            for gid in "abcdef":
                await r.delete(EV.KEY_RUN + gid + "-" + tag)
            await r.aclose()
    asyncio.run(_go())


def test_the_suite_list_keeps_scoreboards_in_time_order_too(monkeypatch):
    """Seen 2026-09-10 after the backfill: a smoke run from the 9th sat above
    run52 of the 10th as 'the latest suite', and a tagged lookup that scans
    limit*8 entries from the head missed a template's older runs entirely."""
    TH = _app()
    if TH is None:
        pytest.skip("app module not importable from THIS checkout here")
    import json
    import uuid
    from Vera.vera.evolve import evolve_capabilities as EV

    async def _go():
        url = os.getenv("REDIS_URL") or ""
        if not url:
            pytest.skip("no REDIS_URL")
        import redis.asyncio as aioredis
        r = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=3, socket_timeout=5)
        if int(r.connection_pool.connection_kwargs.get("db") or 0) == 0:
            pytest.skip("database 0 is prod's")
        try:
            await r.ping()
        except Exception:
            pytest.skip("Redis unreachable")
        monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
        monkeypatch.setattr(EV, "_redis", lambda: r)
        monkeypatch.setattr(EV, "SUITES_CAP", 3)
        tag = uuid.uuid4().hex[:6]
        key = EV.KEY_SUITES
        saved = await r.lrange(key, 0, -1)
        await r.delete(key)
        try:
            async def suite(run, ended):
                row = {"id": "g", "session_id": run + "-" + tag, "status": "done", "wall_s": 1, "ended_at": ended}
                return await TH.cap_evolve_result_ingest(template="default", census_run=run + tag, rows=[row], suite=True)
            assert (await suite("run2", "2026-09-09T00:00:00Z"))["suite"] == "added"
            assert (await suite("run3", "2026-09-10T00:00:00Z"))["suite"] == "added"
            assert (await suite("run1", "2026-09-08T00:00:00Z"))["suite"] == "added", "older: takes its place, not the head"
            assert [json.loads(x)["suite_id"] for x in await r.lrange(key, 0, -1)] == ["run3" + tag, "run2" + tag, "run1" + tag]
            assert (await suite("run0", "2026-09-07T00:00:00Z"))["suite"] == "beyond_window"
            assert (await suite("run2", "2026-09-09T00:00:00Z"))["suite"] == "replaced"
            assert [json.loads(x)["suite_id"] for x in await r.lrange(key, 0, -1)] == ["run3" + tag, "run2" + tag, "run1" + tag]
        finally:
            await r.delete(key)
            for x in reversed(saved):
                await r.lpush(key, x)
            for run in ("run0", "run1", "run2", "run3"):
                await r.delete(EV.KEY_RUN + run + "-" + tag)
                for raw in await r.lrange(EV.KEY_RUNS, 0, -1) or []:
                    try:
                        if json.loads(raw)["run_id"] == run + "-" + tag:
                            await r.lrem(EV.KEY_RUNS, 0, raw)
                    except Exception:
                        pass
            await r.aclose()
    asyncio.run(_go())
