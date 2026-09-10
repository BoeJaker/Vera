"""The census control caps must load with the app — and pausing must round-trip.

Imports the app module the way the loader does, so it runs in-container (where
the merge gate executes pytest) and skips on a host venv, where `Vera.vera.X`
resolves elsewhere. A pure test cannot see an import-time NameError; this can.
"""
import asyncio
import json
import os

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
