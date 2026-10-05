"""A census=finish release waits for a census that is due now - the next arm of a pair - so
both arms run on one build (4 Oct 2026: OFF on 1cb3c8ba, ORDER on 1d9d47de)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rc", ROOT / "vera" / "evolve" / "release_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
REL = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")
SCH = (ROOT / "vera" / "evolve" / "schedule_capabilities.py").read_text(encoding="utf-8")
CLEAR = sorted(R.CLEAR_STATES)[0]


def test_finish_holds_while_a_census_is_due():
    ok, why = R.may_release("finish", {"busy": False, "state": CLEAR, "due": ["Census · intent-core · order"]})
    assert ok is False and "due now" in why and "one build" in why


def test_finish_goes_when_nothing_is_due():
    assert R.may_release("finish", {"busy": False, "state": CLEAR, "due": []})[0] is True
    assert R.may_release("finish", {"busy": False, "state": CLEAR})[0] is True


def test_force_and_goal_ignore_due():
    c = {"busy": False, "state": CLEAR, "due": ["x"]}
    assert R.may_release("force", c)[0] is True
    assert R.may_release("goal", c)[0] is True


def test_the_pending_release_waits_through_decide():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 4, 0, 51, tzinfo=timezone.utc)
    p = {"status": "waiting", "mode": "finish", "requested_at": now.isoformat()}
    act = R.decide(p, {"busy": False, "state": CLEAR, "due": ["arm 2"]}, now=now, process_started_at=None)
    assert act["action"] == "wait"


def test_wiring():
    assert "s[\"due\"] = await sc.census_due_now()" in REL
    assert "async def census_due_now() -> List[str]:" in SCH
    assert 'r.get("kind") == "census" and core.is_due(r, now, free)[0]' in SCH
