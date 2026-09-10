"""The census control caps must load with the app — and pausing must round-trip.

Imports the app module the way the loader does, so it runs in-container (where
the merge gate executes pytest) and skips on a host venv, where `Vera.vera.X`
resolves elsewhere. A pure test cannot see an import-time NameError; this can.
"""
import asyncio
import json
import os
import time

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.census import census_capabilities as CC
except Exception:                                    # pragma: no cover
    ORCH = CC = None

# `Vera` is a namespace package: on the HOST it resolves to the main checkout,
# not to the worktree this test sits in, so a host run would test the wrong
# code (and did, once). Only trust an import that came from THIS tree.
_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(CC is not None and
                  os.path.realpath(getattr(CC, "__file__", "")).startswith(_HERE_ROOT))

pytestmark = pytest.mark.skipif(
    not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


def test_caps_are_registered():
    for name in ("census.control", "census.control.set", "census.runs", "census.live"):
        assert name in ORCH.CAPABILITY_REGISTRY, name


def test_restart_flag_exists_and_defaults_to_resume():
    import inspect
    sig = inspect.signature(ORCH.cap_sys_dev_restart)
    assert "resume_census" in sig.parameters
    assert sig.parameters["resume_census"].default is True


def test_pause_resume_drop_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(CC, "CENSUS_DIR", tmp_path)
    # Nothing live: a restart writes NOTHING (a stale pause would stop the next census).
    plan = run(CC.census_before_restart(True))
    assert plan["action"] == "none"
    assert not (tmp_path / "census.control.json").exists()
    # A live harness report -> a restart pauses (default) ...
    (tmp_path / "census.active.json").write_text(json.dumps({
        "state": "running", "updated_at": CC._ctl._now_iso(),
        "goals_total": 4, "goals_done": 1, "current_goal": "g2", "template": "t"}))
    plan = run(CC.census_before_restart(True))
    assert plan["action"] == "pause" and plan["wrote"]
    ctl = json.loads((tmp_path / "census.control.json").read_text())
    assert ctl["pause"] and ctl["resume_on_start"] and ctl["reason"] == "restart"
    # ... and the startup lift clears exactly that pause.
    lifted = CC._lift_restart_pause_sync()
    assert lifted["lifted"] and lifted["state"] == "run"
    assert not json.loads((tmp_path / "census.control.json").read_text())["pause"]
    # A person's pause is NOT lifted by a startup.
    run(CC.cap_census_control_set(action="pause", reason="operator", by="test"))
    assert CC._lift_restart_pause_sync()["lifted"] is False
    view = run(CC.cap_census_control())
    assert view["state"] == "pause" and view["active"]["live"] is True
    # resume_census=false -> drop.
    plan = run(CC.census_before_restart(False))
    assert plan["action"] == "drop"
    assert run(CC.cap_census_control())["state"] == "drop"
    # Bad action is refused, not written.
    assert run(CC.cap_census_control_set(action="halt"))["ok"] is False


def test_the_restart_waits_for_the_harness_to_acknowledge(tmp_path, monkeypatch):
    """Found live 2026-09-10: the re-exec came 1.5s after the pause was written
    and the new process lifted it 9s later, inside the harness's poll, so the
    harness never saw the pause and sat on a loop the restart had killed."""
    import inspect
    monkeypatch.setattr(CC, "CENSUS_DIR", tmp_path)
    (tmp_path / "census.active.json").write_text(json.dumps({
        "state": "running", "updated_at": CC._ctl._now_iso(),
        "goals_total": 4, "goals_done": 1, "current_goal": "g2", "template": "t"}))
    plan = run(CC.census_before_restart(True))
    assert plan["action"] == "pause" and plan["wrote"] and plan["written_at"]
    assert plan["ack_wait_max_s"] == CC._ctl.PAUSE_ACK_MAX_S
    # the harness says nothing: the wait is bounded and says so
    t0 = time.time()
    out = run(CC.census_wait_acked(plan, max_wait_s=1.5))
    assert out["acked"] is False and out["why"] == "timeout" and 1.0 <= time.time() - t0 < 5
    # the harness parks itself: acknowledged
    (tmp_path / "census.active.json").write_text(json.dumps({
        "state": "paused", "updated_at": CC._ctl._now_iso(), "current_goal": "g2"}))
    out = run(CC.census_wait_acked(plan, max_wait_s=5))
    assert out["acked"] is True and out["waited_s"] < 3
    # nothing written (no live census) -> nothing to wait for
    assert run(CC.census_wait_acked({"action": "none"}))["acked"] is False
    # and sys.dev.restart hands exactly this wait to the re-exec as its gate
    src = inspect.getsource(ORCH.cap_sys_dev_restart)
    assert "census_wait_acked" in src and "gate=_gate" in src
    assert "gate" in inspect.signature(ORCH._do_restart).parameters


def test_the_harness_named_session_outranks_the_loops_staleness_rule(monkeypatch):
    """census.live's `active` blinked to None mid-goal: the loop's staleness rule
    hesitates during a long generation, and a helper read that blink as a gap
    between goals and restarted prod (2026-09-10 21:18Z, a 17-minute goal lost).
    While the harness is live it names the session it is running; a named
    session that still says running is the goal in flight, staleness or not."""
    class _R:
        def __init__(self, runs):
            self.runs = runs
        async def hgetall(self, key):
            sid = key.rsplit(":", 1)[-1]
            return {k.encode(): v.encode() for k, v in (self.runs.get(sid) or {}).items()}
        async def zrevrange(self, key, a, b):
            return [s.encode() for s in self.runs]
    runs = {"named": {"status": "running", "goal": "chart", "started_at": "2026-09-10T22:00:00Z"},
            "other": {"status": "running", "goal": "x", "started_at": "2026-09-10T21:00:00Z"}}
    monkeypatch.setattr(CC, "_redis", lambda: _R(runs))

    async def stale(r, sid, run):
        return True          # the loop's rule hesitates: everything looks stale
    monkeypatch.setattr(CC, "_is_stale", stale)
    got = run(CC._running_loop("named"))
    assert got.get("session_id") == "named" and got.get("named_by_harness") is True, "the harness's word stands"
    assert run(CC._running_loop("")) == {}, "without the harness's name the staleness rule decides, as before"
    assert run(CC._running_loop("gone")) == {}, "a named session that is not running is not the goal in flight"
    runs["named"]["status"] = "done"
    assert run(CC._running_loop("named")) == {}
