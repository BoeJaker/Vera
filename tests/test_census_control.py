"""Census control across restarts + recorded routing (vera/census/control.py).

The rule these guard: a prod restart must not cost a census, and a census row
must say which node and model served it.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.census import control as ct  # noqa: E402


def _ts(offset_s=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


# ── control file ─────────────────────────────────────────────────────────────
def test_make_control_shapes():
    p = ct.make_control("pause", reason="restart", by="sys.dev.restart", resume_on_start=True)
    assert p["pause"] and not p["drop"] and p["resume_on_start"]
    d = ct.make_control("drop", reason="operator said no")
    assert d["drop"] and not d["pause"] and not d["resume_on_start"]
    r = ct.make_control("resume")
    assert not r["pause"] and not r["drop"]
    with pytest.raises(ValueError):
        ct.make_control("stop")


def test_control_state_precedence():
    assert ct.control_state({}) == "run"
    assert ct.control_state({"pause": True}) == "pause"
    assert ct.control_state({"pause": True, "drop": True}) == "drop"


def test_only_a_restart_pause_is_lifted_on_start():
    assert ct.should_lift_on_start(ct.make_control("pause", resume_on_start=True))
    # A pause a PERSON wrote stays until they lift it.
    assert not ct.should_lift_on_start(ct.make_control("pause"))
    assert not ct.should_lift_on_start(ct.make_control("drop"))
    assert not ct.should_lift_on_start({})


def test_write_is_atomic_and_readable(tmp_path):
    p = str(tmp_path / "census.control.json")
    assert ct.write_json(p, ct.make_control("pause", reason="x"))
    assert ct.read_json(p)["pause"] is True
    assert not os.path.exists(p + ".tmp")
    assert ct.read_json(str(tmp_path / "missing.json")) == {}


# ── active file ──────────────────────────────────────────────────────────────
def test_active_view_judges_liveness_from_the_timestamp():
    fresh = {"state": "running", "updated_at": _ts(-60), "goals_total": 12, "goals_done": 3,
             "current_goal": "author-then-edit", "template": "default"}
    v = ct.active_view(fresh)
    assert v["live"] and not v["stale"]
    assert v["progress"] == {"done": 3, "total": 12, "current": "author-then-edit", "remaining": 9}
    dead = dict(fresh, updated_at=_ts(-ct.ACTIVE_STALE_S - 60))
    v2 = ct.active_view(dead)
    assert not v2["live"] and v2["stale"] and v2["state"] == "running"
    assert ct.active_view({})["present"] is False
    assert ct.active_view({"state": "done", "updated_at": _ts()})["live"] is False


def test_restart_plan_pauses_only_a_live_census():
    live = {"state": "running", "updated_at": _ts(-10), "goals_total": 4, "goals_done": 1}
    assert ct.restart_plan(live, resume=True)["action"] == "pause"
    assert ct.restart_plan(live, resume=False)["action"] == "drop"
    # Nothing live: no pause is written, or the NEXT census would obey it.
    assert ct.restart_plan({}, resume=True)["action"] == "none"
    assert ct.restart_plan({"state": "done", "updated_at": _ts()}, resume=True)["action"] == "none"
    stale = dict(live, updated_at=_ts(-ct.ACTIVE_STALE_S - 1))
    assert ct.restart_plan(stale, resume=True)["action"] == "none"


# ── routing on rows ──────────────────────────────────────────────────────────
ROW = {
    "id": "build-simple-code", "status": "done", "wall_s": 427.0,
    "routing": {
        "at_start": {"roles": {
            "coder": {"model": "jaahas/qwen3.5-uncensored", "overridden": True,
                      "declared_model": "qwen2.5-coder:14b"},
            "planner": {"model": "jaahas/qwen3.5-uncensored", "overridden": False}},
            "user_overrides": ["coder"]},
        "calls": {"n": 7, "nodes": {"gpu-250": 7}, "models": {"jaahas/qwen3.5-uncensored": 7},
                  "reroutes": [{"reroute": "cpu spill (GPU 80% resident)"}], "spill_calls": 1,
                  "truncated": False},
    },
}


def test_routing_of_reads_a_row():
    ro = ct.routing_of(ROW)
    assert ro["recorded"] and ro["coder"] == "jaahas/qwen3.5-uncensored"
    assert ro["coder_overridden"] and ro["coder_declared"] == "qwen2.5-coder:14b"
    assert ro["calls"] == 7 and ro["reroutes"] == 1 and ro["spill_calls"] == 1
    assert ro["nodes"] == {"gpu-250": 7}


def test_a_row_from_before_routing_says_so():
    assert ct.routing_of({"id": "x"}) == {"recorded": False}
    assert ct.routing_of({"id": "x", "routing": "junk"}) == {"recorded": False}


def test_rollup_flags_a_coder_that_changed_mid_run():
    other = json.loads(json.dumps(ROW))
    other["routing"]["at_start"]["roles"]["coder"]["model"] = "qwen2.5-coder:14b"
    other["routing"]["calls"]["nodes"] = {"gpu-250": 2, "cpu-246": 3}
    r = ct.routing_rollup([ROW, other, {"id": "old"}])
    assert r["recorded_goals"] == 2
    assert r["coder_changed"] and r["coders"] == ["jaahas/qwen3.5-uncensored", "qwen2.5-coder:14b"]
    assert r["nodes"] == {"gpu-250": 9, "cpu-246": 3}
    assert r["calls"] == 14 and r["reroutes"] == 2 and r["spill_calls"] == 2


def test_every_role_is_reported_not_just_the_coder():
    row = json.loads(json.dumps(ROW))
    row["routing"]["at_start"]["roles"].update({
        "executor": {"model": "", "overridden": False},
        "writer": {"model": "gemma3:12b", "overridden": False}})
    row["routing"]["calls"]["calls"] = [
        {"role": "planner", "job": "loop_planner", "model": "jaahas/qwen3.5-uncensored"},
        {"role": "executor", "job": "loop_executor", "model": "jaahas/qwen3.5-uncensored"},
        {"role": "executor", "job": "loop_executor", "model": "jaahas/qwen3.5-uncensored"},
        {"role": "", "job": "chat", "model": "jaahas/qwen3.5-uncensored"},
        {"role": "", "job": "", "model": "nomic-embed-text"},
        {"role": "writer", "job": "loop_writer", "model": "gemma3:12b"},
    ]
    ro = ct.routing_of(row)
    assert set(ro["roles"]) == {"coder", "planner", "executor", "writer"}
    assert ro["roles"]["writer"]["model"] == "gemma3:12b"
    assert ro["by_role"] == {
        "planner": {"jaahas/qwen3.5-uncensored": 1},
        "executor": {"jaahas/qwen3.5-uncensored": 2},
        "chat": {"jaahas/qwen3.5-uncensored": 1},
        "embed": {"nomic-embed-text": 1},
        "writer": {"gemma3:12b": 1},
    }
    roll = ct.routing_rollup([row, ROW])
    assert roll["roles"]["writer"] == ["gemma3:12b"]
    assert roll["by_role"]["executor"] == {"jaahas/qwen3.5-uncensored": 2}
    assert roll["roles_changed"] == []
    # A role whose model changed between goals is named.
    other = json.loads(json.dumps(row))
    other["routing"]["at_start"]["roles"]["writer"]["model"] = "qwen3.5:9b"
    assert ct.routing_rollup([row, other])["roles_changed"] == ["writer"]


def test_rollup_of_unrecorded_runs_is_empty_not_zero_confidence():
    r = ct.routing_rollup([{"id": "a"}, {"id": "b"}])
    assert r["recorded_goals"] == 0 and r["coders"] == [] and not r["coder_changed"]


# ── the harness must acknowledge a restart's pause before the re-exec ────────
def test_pause_acked_means_paused_since_the_control_was_written():
    written = "2026-09-10T17:11:40Z"
    assert ct.pause_acked({"state": "paused", "updated_at": "2026-09-10T17:11:46Z"}, written)
    assert ct.pause_acked({"state": "paused", "updated_at": written}, written), "same second counts"
    # an older 'paused' is a previous pause, not this one
    assert not ct.pause_acked({"state": "paused", "updated_at": "2026-09-10T15:38:55Z"}, written)
    assert not ct.pause_acked({"state": "running", "updated_at": "2026-09-10T17:11:46Z"}, written)
    assert not ct.pause_acked({}, written)
    assert not ct.pause_acked({"state": "paused"}, written), "no timestamp: cannot be believed"
    # a drop is acknowledged by 'dropped'
    assert ct.pause_acked({"state": "dropped", "updated_at": "2026-09-10T17:11:46Z"}, written, "drop")
    assert not ct.pause_acked({"state": "paused", "updated_at": "2026-09-10T17:11:46Z"}, written, "drop")
    assert 10 <= ct.PAUSE_ACK_MAX_S <= 120, "bounded: a silent harness must not hang the restart"


# ── the running goal's routing, live ─────────────────────────────────────────
def test_live_routing_summarises_the_goals_own_calls():
    entries = [
        {"ts": "2026-09-10T16:20:00Z", "instance": "gpu-250", "model": "q", "tok_per_s": 20.0, "status": "done"},   # before the goal
        {"ts": "2026-09-10T16:21:30Z", "instance": "cpu-246", "model": "nomic-embed-text", "status": "done"},
        {"ts": "2026-09-10T16:22:00Z", "instance": "gpu-250", "model": "q", "tok_per_s": 24.0, "gpu_resident_pct": 100,
         "elapsed_s": 5.2, "job_type": "loop_coder", "status": "done"},
        {"ts": "2026-09-10T16:23:00Z", "instance": "gpu-250", "model": "q", "tok_per_s": 2.0, "gpu_resident_pct": 80,
         "cpu_spill": True, "status": "done"},
        {"ts": "2026-09-10T16:24:00Z", "instance": "cpu-247", "model": "q", "tok_per_s": 1.0, "escalated": True, "status": "done"},
        {"ts": "2026-09-10T16:25:00Z", "instance": "gpu-250", "model": "q", "tok_per_s": 30.0, "status": "done",
         "session_id": "other-session"},                                                                              # someone else's
    ]
    r = ct.live_routing(entries, "2026-09-10T16:21:00Z", session_id="sid-1", last_n=3)
    assert r["calls"] == 4 and r["since"] == "2026-09-10T16:21:00"
    assert r["by_node"] == {"cpu-246": 1, "gpu-250": 2, "cpu-247": 1}
    assert r["by_model"] == {"nomic-embed-text": 1, "q": 3}
    assert r["spill_calls"] == 1 and r["reroutes"] == 1
    assert r["tok_s_median"] == 2.0, "median of 24, 2, 1"
    assert [x["node"] for x in r["last"]] == ["gpu-250", "gpu-250", "cpu-247"] and r["last"][1]["spill"] is True
    assert r["last"][0]["ts"] == "16:22:00" and r["last"][0]["job"] == "loop_coder"
    # a call stamped with the goal's own session counts even from before the window
    r2 = ct.live_routing([{"ts": "2026-09-10T16:00:00Z", "instance": "gpu-250", "model": "q", "session_id": "sid-1"}],
                         "2026-09-10T16:21:00Z", session_id="sid-1")
    assert r2["calls"] == 1
    assert ct.live_routing([], "", session_id="")["calls"] == 0
