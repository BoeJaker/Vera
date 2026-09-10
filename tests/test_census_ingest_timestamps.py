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
