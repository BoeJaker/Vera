"""Loop Lab schedules: windows, what is due, the census gating, the calendar.

The first schedule asked for (2026-09-21): censuses back to back on weekdays
05:00-17:00. These tests pin what that means - a census starts only inside
the window, never beside a census or a loop, the next one follows the last
after a cooldown, and the window closing lets the one in flight finish (or
yields / drops it when asked). Pure: no Redis, no clock, no harness.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.evolve import schedule_core as sc  # noqa: E402

UTC = timezone.utc
FREE = {"census_running": False, "loops_running": 0, "harness_blocked": ""}


def _t(y, m, d, hh, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def weekday(**over):
    rec = {"kind": "census", "target": {"template": "default"}, "timezone": "UTC"}
    rec.update(over)
    return sc.normalize(rec, now=_t(2026, 9, 21, 0))


# 2026-09-21 is a Monday.
MON, SAT = _t(2026, 9, 21, 9), _t(2026, 9, 26, 9)


def test_weekday_census_defaults_and_window():
    r = sc.weekday_census(timezone_name="UTC", now=_t(2026, 9, 21, 0))
    assert r["days"] == [0, 1, 2, 3, 4] and r["start"] == "05:00" and r["end"] == "17:00"
    assert r["repeat"] == "continuous" and r["at_window_end"] == "finish"
    assert sc.current_window(r, MON) == (_t(2026, 9, 21, 5), _t(2026, 9, 21, 17))
    assert sc.current_window(r, SAT) is None
    assert sc.current_window(r, _t(2026, 9, 21, 4, 59)) is None
    assert sc.current_window(r, _t(2026, 9, 21, 17, 0)) is None
    # Sunday night: the next window is Monday's.
    nw = sc.next_window(r, _t(2026, 9, 20, 22))
    assert nw == (_t(2026, 9, 21, 5), _t(2026, 9, 21, 17))


def test_census_is_due_only_when_the_box_is_free():
    r = weekday()
    assert sc.is_due(r, MON, FREE) == (True, "due")
    assert sc.is_due(r, SAT, FREE)[1] == "outside window"
    assert sc.is_due(r, MON, dict(FREE, census_running=True))[1] == "census in flight"
    assert sc.is_due(r, MON, dict(FREE, loops_running=1))[1] == "an agent loop is running"
    assert sc.is_due(r, MON, dict(FREE, harness_blocked="partial run files present"))[1] == \
        "partial run files present"
    assert sc.is_due(dict(r, enabled=False), MON, FREE)[1] == "disabled"


def test_continuous_census_chains_after_cooldown():
    r = weekday(cooldown_minutes=2)
    r["last_started_at"] = sc.iso(_t(2026, 9, 21, 6))
    r["last_finished_at"] = sc.iso(_t(2026, 9, 21, 8, 59))
    assert sc.is_due(r, _t(2026, 9, 21, 9, 0), FREE)[1] == "cooling down"
    assert sc.is_due(r, _t(2026, 9, 21, 9, 2), FREE) == (True, "due")


def test_once_per_window_and_every():
    s = weekday(kind="suite", target={"tag": "nightly"}, repeat="once_per_window")
    assert sc.is_due(s, MON, FREE) == (True, "due")
    s["last_started_at"] = sc.iso(_t(2026, 9, 21, 5, 1))
    assert sc.is_due(s, MON, FREE)[1] == "fired this window"
    assert sc.is_due(s, _t(2026, 9, 22, 9), FREE) == (True, "due")     # Tuesday's window
    e = weekday(kind="task", target={"id": "t1"}, repeat="every", every_minutes=30)
    e["last_started_at"] = sc.iso(_t(2026, 9, 21, 8, 45))
    assert sc.is_due(e, MON, FREE)[1] == "interval not elapsed"
    assert sc.is_due(e, _t(2026, 9, 21, 9, 15), FREE) == (True, "due")
    # An exclusive non-census schedule waits for the census; a non-exclusive one does not.
    assert sc.is_due(e, _t(2026, 9, 21, 9, 15), dict(FREE, census_running=True))[1] == "census in flight"
    assert sc.is_due(dict(e, exclusive=False), _t(2026, 9, 21, 9, 15), dict(FREE, census_running=True))[0]


def test_one_shot():
    o = sc.normalize({"kind": "task", "target": {"id": "t1"}, "once_at": "2026-09-21T10:00:00Z",
                      "timezone": "UTC"}, now=_t(2026, 9, 21, 0))
    assert o["days"] == [] and o["once_at"] == "2026-09-21T10:00:00+00:00"
    assert sc.is_due(o, _t(2026, 9, 21, 9, 59), FREE)[1] == "not yet"
    assert sc.is_due(o, _t(2026, 9, 21, 10), FREE) == (True, "due")
    o["last_started_at"] = sc.iso(_t(2026, 9, 21, 10))
    assert sc.is_due(o, _t(2026, 9, 21, 11), FREE)[1] == "already fired"


def test_plan_tick_starts_one_census_and_fences_exclusive_work():
    c1, c2 = weekday(title="c1"), weekday(title="c2")
    s = weekday(kind="suite", target={"tag": "x"}, repeat="once_per_window")
    n = weekday(kind="cap", target={"name": "obs.provenance"}, repeat="once_per_window", exclusive=False)
    plan = sc.plan_tick([c1, c2, s, n], MON, FREE)
    kinds = [(p["schedule_id"], p["kind"]) for p in plan]
    assert kinds == [(c1["id"], "census"), (n["id"], "cap")]      # one census; suite fenced; cap not
    assert sc.plan_tick([c1, c2, s, n], MON, dict(FREE, census_running=True)) == \
        [{"schedule_id": n["id"], "kind": "cap", "target": n["target"], "title": n["title"], "reason": "due"}]
    assert len(sc.plan_tick([n, n, n, n], MON, FREE, max_starts=2)) == 2


def test_parked_on_our_own_yield_resumes_when_the_window_is_open():
    r = weekday(at_window_end="yield")
    parked = dict(FREE, census_running=True, census_parked_by_us=True, census_owner=r["id"])
    assert sc.is_due(r, MON, parked) == (True, "resume")
    assert sc.is_due(r, SAT, parked)[1] == "outside window"
    other = weekday()
    assert sc.is_due(other, MON, parked)[1] == "census in flight"
    plan = sc.plan_tick([other, r], MON, parked)
    assert [p["reason"] for p in plan] == ["resume"]


def test_once_at_is_in_the_records_timezone_and_never_window_ends():
    o = sc.normalize({"kind": "census", "target": {}, "once_at": "2026-09-22T09:00", "timezone": "+01:00",
                      "at_window_end": "yield"}, now=_t(2026, 9, 21, 0))
    assert o["once_at"] == "2026-09-22T09:00:00+01:00"
    assert sc.is_due(o, _t(2026, 9, 22, 7, 59), FREE)[1] == "not yet"
    assert sc.is_due(o, _t(2026, 9, 22, 8, 0), FREE) == (True, "due")
    st = dict(FREE, census_running=True, census_owner=o["id"])
    assert sc.window_end_action(o, _t(2026, 9, 22, 8, 5), st) is None


def test_window_end_action_is_applied_once():
    y = weekday(at_window_end="drop")
    st = dict(FREE, census_running=True, census_owner=y["id"])
    assert sc.window_end_action(y, _t(2026, 9, 21, 17, 1), st)["action"] == "drop"
    assert sc.window_end_action(y, _t(2026, 9, 21, 17, 1), dict(st, window_end_applied=True)) is None


def test_window_end_action_only_for_the_owner_after_the_window():
    y = weekday(at_window_end="yield")
    running_mine = dict(FREE, census_running=True, census_owner=y["id"])
    assert sc.window_end_action(y, _t(2026, 9, 21, 16, 59), running_mine) is None
    act = sc.window_end_action(y, _t(2026, 9, 21, 17, 1), running_mine)
    assert act and act["action"] == "yield"
    assert sc.window_end_action(y, _t(2026, 9, 21, 17, 1), dict(running_mine, census_owner="other")) is None
    assert sc.window_end_action(weekday(), _t(2026, 9, 21, 17, 1), running_mine) is None   # finish
    assert sc.window_end_action(y, _t(2026, 9, 21, 17, 1), FREE) is None


def test_projection_one_event_per_weekday():
    r = weekday(title="Census · default")
    ev = sc.project_events([r], _t(2026, 9, 21, 0), _t(2026, 9, 27, 23, 59))
    days = [e["start"][:10] for e in ev]
    assert days == ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
    assert ev[0]["id"] == f"sched:{r['id']}:2026-09-21" and ev[0]["read_only"] and ev[0]["source"] == "loop-lab"
    assert ev[0]["start"].endswith("05:00:00+00:00") and ev[0]["end"].endswith("17:00:00+00:00")
    runs = [{"id": "r1", "schedule_id": r["id"], "kind": "census", "title": "Census", "result": "done",
             "started_at": "2026-09-21T05:00:00Z", "finished_at": "2026-09-21T08:00:00Z"},
            {"id": "old", "kind": "census", "started_at": "2026-09-01T05:00:00Z"}]
    h = sc.history_events(runs, _t(2026, 9, 21, 0), _t(2026, 9, 27, 0))
    assert [e["id"] for e in h] == ["run:r1"] and h[0]["source"] == "loop-lab-run"


def test_normalize_rejects_what_a_person_must_fix():
    with pytest.raises(ValueError):
        sc.normalize({"kind": "nope", "target": {}})
    with pytest.raises(ValueError):
        sc.normalize({"kind": "suite", "target": {}})
    with pytest.raises(ValueError):
        sc.normalize({"kind": "census", "target": {}, "start": "17:00", "end": "05:00", "timezone": "UTC"})
    with pytest.raises(ValueError):
        sc.normalize({"kind": "census", "target": {}, "days": [7], "timezone": "UTC"})
    with pytest.raises(ValueError):
        sc.normalize({"kind": "cap", "target": {"name": "sys.dev.restart"}})
    with pytest.raises(ValueError):
        sc.normalize({"kind": "cap", "target": {"name": "evolve.bleeding_edge.promote_to_main"}})
    p = sc.normalize({"kind": "pipeline", "target": {"id": "abc", "action": "promote"}, "timezone": "UTC"})
    assert p["target"]["to"] == "bleeding-edge"            # never main
    with pytest.raises(ValueError):
        sc.normalize({"kind": "pipeline", "target": {"action": "adopt"}})
    assert sc.cap_denied("sys.dev.restart") and sc.cap_denied("evolve.pipeline.promote")
    assert sc.cap_denied("cluster.job.stop") and sc.cap_denied("jobs.purge_pending")
    assert not sc.cap_denied("evolve.suite.start") and not sc.cap_denied("obs.provenance")


def test_fixed_offset_timezone_moves_the_window():
    # 05:00 at +01:00 is 04:00 UTC.
    r = weekday(timezone="+01:00")
    w = sc.current_window(r, _t(2026, 9, 21, 4, 30))
    assert w and w[0].astimezone(UTC) == _t(2026, 9, 21, 4)
    assert sc.current_window(r, _t(2026, 9, 21, 3, 30)) is None


def test_named_zone_when_available():
    try:
        sc.resolve_tz("Europe/London")
    except ValueError:
        pytest.skip("no zoneinfo on this interpreter")
    r = weekday(timezone="Europe/London")
    # BST in September: 05:00 London = 04:00 UTC.
    w = sc.current_window(r, _t(2026, 9, 21, 4, 30))
    assert w and w[0].astimezone(UTC) == _t(2026, 9, 21, 4)


def test_results_mode_projects_runs_and_goals():
    suites = [{"suite_id": "run57", "tag": "census-default", "started_at": "2026-09-21T04:07:09Z",
               "ts": "2026-09-21T07:46:10Z", "tasks_n": 3, "done": 2, "capped": 1, "pass_rate": 0.868,
               "results": [{"task": "census-default-a", "label": "a", "status": "done", "elapsed_s": 100, "checks": "5/5"},
                           {"task": "census-default-b", "label": "b", "status": "wall-cap", "elapsed_s": 1800, "hit_cap": True},
                           {"task": "census-default-c", "label": "c", "status": "done", "elapsed_s": 60}]},
              {"suite_id": "old", "tag": "nightly", "started_at": "2026-09-01T00:00:00Z", "ts": "2026-09-01T01:00:00Z",
               "tasks_n": 1, "pass_rate": 0.2, "results": []}]
    ev = sc.results_events(suites, _t(2026, 9, 20, 0), _t(2026, 9, 27, 0))
    assert [e["id"] for e in ev] == ["result:run57"]
    e = ev[0]
    assert e["kind"] == "census" and e["grade"] == "good" and e["color"] == sc.RESULT_COLORS["good"]
    assert e["title"].startswith("run57 · 2/3 · 87%") and "1 capped" in e["title"]
    assert e["start"] == "2026-09-21T04:07:09+00:00" and e["end"] == "2026-09-21T07:46:10+00:00"
    assert len(e["rows"]) == 3 and e["rows"][1]["hit_cap"] is True
    goals = sc.results_events(suites, _t(2026, 9, 20, 0), _t(2026, 9, 27, 0), granularity="goals")
    assert [g["id"] for g in goals] == ["result:run57:a", "result:run57:b", "result:run57:c"]
    assert goals[0]["end"] == "2026-09-21T04:08:49+00:00"          # placed by elapsed time, in order
    assert goals[1]["grade"] == "bad" and goals[1]["start"] == goals[0]["end"]
    assert sc.result_grade(None) == "none" and sc.result_grade(0.7) == "mixed" and sc.result_grade(0.1) == "bad"


# ── any Loop Lab work, tied to board items, drawn in layers (2026-09-28) ─────

def test_loop_and_tests_kinds_normalise():
    lp = sc.normalize({"kind": "loop", "target": {"goal": "tidy the census notes"}, "timezone": "UTC"})
    assert lp["target"]["profile"] == "coding" and lp["title"].startswith("Loop · tidy")
    with pytest.raises(ValueError):
        sc.normalize({"kind": "loop", "target": {}, "timezone": "UTC"})
    ts = sc.normalize({"kind": "tests", "target": {"markers": "critical"}, "timezone": "UTC"})
    assert ts["target"]["branch"] == "bleeding-edge" and ts["target"]["paths"] == "tests"
    assert ts["title"] == "Tests · bleeding-edge · critical"


def test_a_background_run_holds_the_next_start():
    rec = sc.normalize({"kind": "tests", "target": {}, "timezone": "UTC", "days": [0], "start": "08:00",
                        "end": "18:00", "repeat": "every", "every_minutes": 30})
    rec["last_started_at"] = sc.iso(MON - timedelta(hours=1))
    assert sc.is_due(rec, MON, FREE) == (False, "previous run still going")
    rec["last_finished_at"] = sc.iso(MON - timedelta(minutes=20))
    assert sc.is_due(rec, MON, FREE)[0] is True
    # a run record lost long ago does not hold the schedule forever
    rec["last_finished_at"] = ""
    rec["last_started_at"] = sc.iso(MON - timedelta(hours=sc.STILL_RUNNING_HOURS + 1))
    assert sc.is_due(rec, MON, FREE)[0] is True


def test_board_link_on_any_kind():
    b = sc.normalize({"kind": "board", "target": {"id": "abc"}, "timezone": "UTC"})
    assert b["board_id"] == "abc"
    c = weekday(board_id="xyz")
    assert c["board_id"] == "xyz"
    assert [s["id"] for s in sc.for_board([b, c, weekday()], "xyz")] == [c["id"]]
    assert [s["id"] for s in sc.for_board([b, c], "abc")] == [b["id"]]
    ev = sc.project_events([c], MON - timedelta(days=1), MON + timedelta(days=1))
    assert ev and all(e["board_id"] == "xyz" for e in ev)


def test_layer_of_every_event():
    assert sc.layer_of({"source": "results"}) == "results"
    assert sc.layer_of({"source": "loop-lab", "kind": "census"}) == "windows"
    assert sc.layer_of({"source": "loop-lab-run", "kind": "suite"}) == "runs"
    assert sc.layer_of({"source": "loop-lab", "kind": "census", "board_id": "b1"}) == "board"
    assert sc.layer_of({"source": "loop-lab-run", "kind": "board"}) == "board"
    assert sc.layer_of({"source": "local"}) == "calendar" and sc.layer_of({"source": "google"}) == "calendar"
    assert set(sc.LAYERS) == {"windows", "runs", "results", "board", "calendar"}
