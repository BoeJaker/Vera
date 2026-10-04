"""A release waits while an agent loop is running in the process it would restart
(4 Oct 2026 15:16: a release restarted prod 24 min into a probe loop; the run was lost and
left marked 'running', blocking every later probe). force still overrides."""

import importlib.util
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rc_loops", ROOT / "vera" / "evolve" / "release_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
REL = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")
CLEAR = sorted(R.CLEAR_STATES)[0]


class _Task:
    def __init__(self, done):
        self._d = done

    def done(self):
        return self._d


class _Broken:
    def done(self):
        raise RuntimeError("x")


def test_live_loop_ids_are_the_unfinished_tasks():
    tasks = {"aaaa1111": _Task(False), "bbbb2222": _Task(True), "cccc3333": None, "dddd4444": _Broken()}
    assert R.live_loop_ids(tasks) == ["aaaa1111"]
    assert R.live_loop_ids({}) == [] and R.live_loop_ids(None) == []


def test_finish_and_goal_hold_while_a_loop_runs():
    c = {"busy": False, "state": CLEAR, "loops": ["67d0975e-c3c9-4752"]}
    for mode in ("finish", "goal"):
        ok, why = R.may_release(mode, c)
        assert ok is False, mode
        assert "1 agent loop(s) running (67d0975e)" in why and "force" in why


def test_force_overrides_and_no_loops_goes():
    assert R.may_release("force", {"busy": False, "state": CLEAR, "loops": ["x"]})[0] is True
    assert R.may_release("finish", {"busy": False, "state": CLEAR, "loops": []})[0] is True
    assert R.may_release("finish", {"busy": False, "state": CLEAR})[0] is True


def test_a_busy_census_is_still_reported_as_the_census():
    ok, why = R.may_release("finish", {"busy": True, "state": "running", "goal": "g1", "loops": ["x"]})
    assert ok is False and why.startswith("census goal in flight")


def test_the_pending_release_waits_through_decide():
    now = datetime(2026, 10, 4, 14, 16, tzinfo=timezone.utc)
    p = {"status": "waiting", "mode": "finish", "requested_at": now.isoformat()}
    act = R.decide(p, {"busy": False, "state": CLEAR, "loops": ["67d0975e"]}, now=now, process_started_at=None)
    assert act["action"] == "wait"


def test_census_summary_reports_live_loops():
    body = REL[REL.index("async def census_summary()"):]
    body = body[:body.index("\n\n\nasync def ")]
    assert 'core.live_loop_ids(getattr(dm, "_AGENT_LOOP_TASKS", None) or {})' in body
    assert 's["loops"] = ids' in body          # + mcp-started runs (test_release_sees_mcp_loops)
    assert '"dag_workshop_capabilities"' in body and '"Vera.vera.dag.dag_workshop_capabilities"' in body
