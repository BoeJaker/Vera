"""Recover and protect (owner, 18 Oct 2026: tests "can recover if vera restarts and can be properly gated and protected
like the censuses are"; that morning a release restarted prod mid-run and the memory census was closed 'interrupted'
and never re-run). Both are off unless an entry sets them."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import release_core as rel  # noqa: E402
from vera.evolve import schedule_core as core  # noqa: E402

pytestmark = pytest.mark.critical
UTC = timezone.utc
NOW = datetime(2026, 10, 18, 10, 30, tzinfo=UTC)


def _rec(**kw):
    base = {"kind": "cap", "target": {"name": "memory.perf.probe"}, "timezone": "UTC", "days": list(range(7)),
            "start": "10:00", "end": "11:00", "repeat": "once_per_window"}
    base.update(kw)
    return core.normalize(base)


def test_the_new_fields_default_off():
    r = _rec()
    assert r["recover"] is False and r["protect"] is False and r["max_recoveries"] == 2
    assert r["recover_due"] == "" and r["recoveries"] == 0 and r["protect_minutes"] == 120


def test_an_interrupted_recurring_run_is_due_again_in_the_same_window():
    r = _rec(recover=True, last_started_at="2026-10-18T10:05:00+00:00", last_finished_at="2026-10-18T10:07:00+00:00")
    assert core.is_due(r, NOW, {}) == (False, "fired this window")
    assert core.recover_after_interrupt(r, "2026-10-18T10:07:00+00:00") is True
    assert core.is_due(r, NOW, {}) == (True, "recover")
    # ... and still waits for the box: a census in flight, an exclusive run, a declared probe
    assert core.is_due(r, NOW, {"census_running": True})[0] is False
    assert core.is_due(r, NOW, {"exclusive_running": 1})[0] is False
    assert core.is_due(r, NOW, {"declared_busy": "probe x"})[0] is False


def test_an_interrupted_once_entry_fires_again_and_outside_its_window():
    r = _rec(recover=True, once_at="2026-10-18T10:05:00+00:00", last_started_at="2026-10-18T10:05:10+00:00")
    assert core.is_due(r, NOW, {}) == (False, "already fired")
    core.recover_after_interrupt(r, "2026-10-18T10:07:00+00:00")
    assert core.is_due(r, NOW, {}) == (True, "recover")
    w = _rec(recover=True, recover_due="x")
    assert core.is_due(w, NOW + timedelta(hours=3), {}) == (True, "recover")   # past 11:00, still recovers


def test_recoveries_are_counted_so_a_run_that_kills_prod_cannot_loop():
    r = _rec(recover=True, max_recoveries=2)
    assert core.recover_after_interrupt(r, "a") and core.recover_after_interrupt(r, "b")
    assert core.recover_after_interrupt(r, "c") is False and r["recoveries"] == 2
    assert core.recover_after_interrupt(_rec(), "a") is False                    # not a recovering entry


def test_a_protected_entry_waits_while_a_release_drains():
    r = _rec(protect=True)
    assert core.is_due(r, NOW, {"release_draining": True})[0] is False
    assert core.is_due(_rec(), NOW, {"release_draining": True}) == (True, "due")


def test_open_protected_runs_hold_a_restarting_release_for_a_bounded_time():
    scheds = {"p": _rec(id="p", protect=True, title="memory census"), "q": _rec(id="q")}
    runs = [{"id": "r1", "schedule_id": "p", "kind": "cap", "started_at": "2026-10-18T10:20:00+00:00", "finished_at": ""},
            {"id": "r2", "schedule_id": "q", "kind": "cap", "started_at": "2026-10-18T10:20:00+00:00", "finished_at": ""},
            {"id": "r3", "schedule_id": "p", "kind": "cap", "started_at": "2026-10-18T10:00:00+00:00",
             "finished_at": "2026-10-18T10:10:00+00:00"},
            {"id": "r4", "schedule_id": "p", "kind": "cap", "started_at": "2026-10-18T06:00:00+00:00", "finished_at": ""}]
    prot = core.protected_runs(runs, scheds, NOW)
    assert [p["run_id"] for p in prot] == ["r1"]                # r2 unprotected, r3 done, r4 past 120 min
    ok, why = rel.may_release("goal", {"state": "none", "protected": prot})
    assert not ok and "1 protected test run(s) going (memory census)" in why and rel.waiting_on_gates(why)
    assert rel.may_release("goal", {"state": "none", "protected": prot}, restart=False)[0] is True
    assert rel.may_release("force", {"state": "none", "protected": prot})[0] is True
    assert rel.may_release("goal", {"state": "none", "protected": []})[0] is True


def test_the_scheduler_wires_it_in():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "schedule_capabilities.py"),
               encoding="utf-8").read()
    rec = src[src.index("async def _reconcile_runs"):src.index("async def _reconcile_startup")]
    assert "core.recover_after_interrupt(rec, fin)" in rec
    assert 'rec["recover_due"] = ""' in src and 'cur["recoveries"] = 0' in src
    assert 'st["release_draining"]' in src and "async def protected_running()" in src
    rc = open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "release_capabilities.py"),
              encoding="utf-8").read()
    assert 's["protected"] = await sc.protected_running()' in rc
