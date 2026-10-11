"""DEEP-21 D21-3 (compute-roles, 11 Oct 2026): the census harness refreshes census.active.json every
HEARTBEAT_S while running or parked, so a long goal or park never reads as a dead harness."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_heartbeat_thread_refreshes_while_running_or_parked():
    assert "HEARTBEAT_S = 60" in SRC
    i = SRC.index("def _heartbeat_loop(")
    body = SRC[i:i + 300]
    assert 'if _ACTIVE.get("state") in ("running", "paused"):' in body and "write_active()" in body
    assert 'threading.Thread(target=_heartbeat_loop, name="census-heartbeat", daemon=True)' in SRC


def test_writes_are_locked_and_the_thread_starts_with_the_first_goal():
    assert "with _ACTIVE_LOCK:" in SRC
    assert '_HEARTBEAT["started"] = bool(start_heartbeat())' in SRC
