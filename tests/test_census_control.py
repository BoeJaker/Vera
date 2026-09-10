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
