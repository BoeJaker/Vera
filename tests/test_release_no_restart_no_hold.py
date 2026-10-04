"""A merge without a restart kills nothing, so running loops and gates must not hold it; a
cancelled release lets held gates start (4 Oct 2026: a restart=false merge waited on two
gates while its drain key made every new gate wait)."""
import importlib.util
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rc_norestart", ROOT / "vera" / "evolve" / "release_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
REL = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")
CLEAR = sorted(R.CLEAR_STATES)[0]
BUSY_BOX = {"busy": False, "state": CLEAR, "loops": ["s1"],
            "gates": [{"id": "g1", "branch": "fix/a", "state": "running"}]}


def test_no_restart_is_not_held_by_loops_or_gates():
    assert R.may_release("finish", BUSY_BOX, restart=False)[0] is True
    ok, why = R.may_release("finish", BUSY_BOX)                  # default: restarting
    assert ok is False and "loop" in why


def test_a_census_still_holds_a_no_restart_merge():
    ok, why = R.may_release("finish", {"busy": True, "state": "running", "goal": "g"}, restart=False)
    assert ok is False and why.startswith("census goal in flight")


def test_decide_reads_the_pending_release_restart_flag():
    now = datetime(2026, 10, 4, 20, 20, tzinfo=timezone.utc)
    base = {"status": "waiting", "mode": "finish", "requested_at": now.isoformat()}
    assert R.decide(dict(base, restart=False), BUSY_BOX, now=now, process_started_at=None)["action"] == "release"
    assert R.decide(dict(base, restart=True), BUSY_BOX, now=now, process_started_at=None)["action"] == "wait"


def test_release_prod_passes_restart_and_cancel_clears_the_drain():
    assert "core.may_release(mode, summary, restart=bool(restart))" in REL
    cancel = REL[REL.index("async def cap_release_cancel("):]
    cancel = cancel[:cancel.index("\n\n\n")]
    assert "await _drain(False)" in cancel
