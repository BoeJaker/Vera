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


def test_a_restart_keeps_a_persons_pause():
    """A paused harness still reads as live, and a restart's own pause lifts
    itself on startup: it must not replace a pause a person wrote, or the
    census resumes on a GPU they asked for (2026-09-10 21:27Z)."""
    paused = {"state": "paused", "updated_at": _ts(-10), "goals_total": 4, "goals_done": 3}
    theirs = ct.make_control("pause", reason="user needs the GPU", by="claude")
    plan = ct.restart_plan(paused, resume=True, control=theirs)
    assert plan["action"] == "none" and "kept" in plan and "claude" in plan["why"]
    assert not ct.should_lift_on_start(theirs), "and startup leaves it alone"
    # A restart's own earlier pause is not a person's: a second restart may write again.
    ours = ct.make_control("pause", reason=ct.RESTART_REASON, by="sys.dev.restart", resume_on_start=True)
    assert ct.restart_plan(paused, resume=True, control=ours)["action"] == "pause"
    # A running harness with no control on file: unchanged.
    live = {"state": "running", "updated_at": _ts(-10), "goals_total": 4, "goals_done": 1}
    assert ct.restart_plan(live, resume=True, control={})["action"] == "pause"
    assert ct.restart_plan(live, resume=True)["action"] == "pause"
    # A drop on file is not a pause to keep.
    dropped = ct.make_control("drop", reason="x", by="someone")
    assert ct.restart_plan(live, resume=False, control=dropped)["action"] == "drop"
    # A caller who asked not to resume still drops: their pause does not shield the census from an explicit drop.
    assert ct.restart_plan(paused, resume=False, control=theirs)["action"] == "drop"


# ── yield: a pause that waits for the goal boundary ──────────────────────────
def test_a_yield_is_a_pause_that_takes_effect_after_the_goal():
    """The polite pause (2026-09-19): other tests need the box, but a cancelled
    goal is a wasted half hour and a re-run. On file a yield IS a pause, so
    every reader that only asks "is a pause on file" keeps working; the
    harness alone reads `after_goal` and lets the goal in flight finish."""
    y = ct.make_control("yield", reason="running the operator regressions", by="claude")
    assert y["pause"] and y["after_goal"] and not y["drop"] and not y["resume_on_start"]
    assert ct.control_state(y) == "yield"
    # An ordinary pause carries the flag, false, so a reader never KeyErrors on it.
    p = ct.make_control("pause")
    assert p["after_goal"] is False and ct.control_state(p) == "pause"
    # Precedence: a drop still outranks it; a resume clears it.
    assert ct.control_state(dict(y, drop=True)) == "drop"
    r = ct.make_control("resume")
    assert not r["pause"] and not r["after_goal"] and ct.control_state(r) == "run"
    # A person's yield is a person's pause: startup never lifts it.
    assert not ct.should_lift_on_start(y)
    # The harness acknowledges a yield the way it acknowledges a pause: by
    # parking (state=paused) at or after the control was written.
    assert ct.pause_acked({"state": "paused", "updated_at": y["ts"]}, y["ts"])
    assert not ct.pause_acked({"state": "running", "updated_at": y["ts"]}, y["ts"]), \
        "still finishing the goal: not yet acknowledged"


def test_a_restart_under_a_live_yield_pauses_and_restores_it():
    """A yield with the goal still in flight means the harness has NOT parked.
    A restart that 'kept' it (as it keeps a person's pause) would kill the loop
    under the harness and the goal would be recorded as cancelled. So the
    restart pauses properly - the harness cancels and acks, the goal re-runs -
    and puts the yield back on startup, so the census parks after that goal
    instead of running on a box someone asked for."""
    theirs = ct.make_control("yield", reason="need the GPU", by="claude")
    running = {"state": "running", "updated_at": _ts(-10), "goals_total": 4, "goals_done": 1}
    plan = ct.restart_plan(running, resume=True, control=theirs)
    assert plan["action"] == "pause" and plan["restore"] == {
        "after_goal": True, "reason": "need the GPU", "by": "claude"}
    ours = ct.make_control("pause", reason=ct.RESTART_REASON, by="sys.dev.restart",
                           resume_on_start=True, restore=plan["restore"])
    assert ours["pause"] and ours["resume_on_start"] and ours["restore"]["after_goal"]
    assert ct.should_lift_on_start(ours), "the restart's pause still lifts itself"
    lifted = ct.lift_control(ours)
    assert ct.control_state(lifted) == "yield" and lifted["by"] == "claude" \
        and lifted["reason"] == "need the GPU", "...into the yield it displaced, not a resume"
    # Once the harness HAS parked on the yield it is a person's pause like any other: kept.
    parked = {"state": "paused", "updated_at": _ts(-10), "goals_total": 4, "goals_done": 2}
    assert ct.restart_plan(parked, resume=True, control=theirs)["action"] == "none"
    # A plain restart pause (no yield displaced) still lifts into a resume.
    plain = ct.make_control("pause", reason=ct.RESTART_REASON, by="sys.dev.restart", resume_on_start=True)
    assert "restore" not in plain and ct.control_state(ct.lift_control(plain)) == "run"
    # `restore` rides only on a restart's pause: a person's pause never carries one.
    assert "restore" not in ct.make_control("pause", restore={"after_goal": True})
    # A caller who asked not to resume still drops, yield or no yield.
    assert ct.restart_plan(running, resume=False, control=theirs)["action"] == "drop"


# ── /health: is the box free? ────────────────────────────────────────────────
def test_health_summary_says_busy_only_while_a_goal_is_in_flight():
    """An agent reads /health before GPU work. `busy` must be true exactly
    when a census goal is running (or a yield is requested but not yet
    parked - the goal is still running), and false for a parked, finished,
    absent or dead harness, so it neither taints a row nor waits on a ghost."""
    def view(**a):
        base = {"updated_at": _ts(-10), "goals_total": 12, "goals_done": 3,
                "current_goal": "author-then-edit", "template": "default", "census_run": "run54"}
        base.update(a)
        return ct.active_view(base)
    run = ct.make_control("resume")
    h = ct.health_summary(run, view(state="running"))
    assert h["busy"] and h["state"] == "running" and h["control"] == "run"
    assert h["goal"] == "author-then-edit" and h["done"] == 3 and h["total"] == 12 \
        and h["template"] == "default" and h["census_run"] == "run54" and "advice" in h
    # yield requested, goal still running: still busy, and it says why
    y = ct.make_control("yield", by="claude")
    h = ct.health_summary(y, view(state="running"))
    assert h["busy"] and h["state"] == "yielding" and h["by"] == "claude"
    # parked on the yield: free
    h = ct.health_summary(y, view(state="paused", pause_kind="yield"))
    assert not h["busy"] and h["state"] == "yielded" and "advice" not in h
    # a plain pause, parked: free; a plain pause not yet acted on: busy, 'pausing'
    p = ct.make_control("pause", by="sys.dev.restart", resume_on_start=True)
    assert ct.health_summary(p, view(state="paused"))["state"] == "paused"
    assert ct.health_summary(p, view(state="running")) == dict(
        ct.health_summary(p, view(state="running")), busy=True, state="pausing")
    # no harness at all, a finished one, a dead one
    assert ct.health_summary({}, ct.active_view({})) == {"busy": False, "state": "none", "control": "run"}
    assert not ct.health_summary(run, view(state="done"))["busy"]
    dead = ct.health_summary(run, view(state="running", updated_at=_ts(-ct.ACTIVE_STALE_S - 60)))
    assert not dead["busy"] and dead["state"] == "stale"


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
    # the last calls are GENERATION calls; an embedding burst does not hide them
    r3 = ct.live_routing(entries + [{"ts": "2026-09-10T16:26:0%dZ" % k, "instance": "cpu-246", "model": "nomic-embed-text"} for k in range(5)],
                         "2026-09-10T16:21:00Z", session_id="sid-1", last_n=3)
    assert r3["calls"] == 9 and [x["model"] for x in r3["last"]] == ["q", "q", "q"]
    only_embed = ct.live_routing([{"ts": "2026-09-10T16:26:00Z", "instance": "cpu-246", "model": "nomic-embed-text"}], "2026-09-10T16:21:00Z")
    assert [x["model"] for x in only_embed["last"]] == ["nomic-embed-text"], "nothing else: show what there is"
    # a call stamped with the goal's own session counts even from before the window
    r2 = ct.live_routing([{"ts": "2026-09-10T16:00:00Z", "instance": "gpu-250", "model": "q", "session_id": "sid-1"}],
                         "2026-09-10T16:21:00Z", session_id="sid-1")
    assert r2["calls"] == 1
    assert ct.live_routing([], "", session_id="")["calls"] == 0
