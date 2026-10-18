"""FM-4.3 instrumentation (compute-roles, 12 Oct 2026): census rows say whether plan.tier_lower was on
and whether it CHANGED the goal's tier - the 12 Oct arm never fired once and nothing said so."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import loop_trace_core as T  # noqa: E402

DWC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
RC = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_decision_and_event_carry_applied():
    assert "_lower_applied = bool(_lower) and heur != _v7_tier_heuristic(goal)" in DWC
    assert '"tier_lower_applied": _lower_applied' in DWC
    assert '"tier_lower_applied": bool(tier_info.get("tier_lower_applied"))})' in DWC


def test_rows_carry_it():
    assert '"tier_lower_applied": (tr.get("plan") or {}).get("tier_lower_applied"),' in RC


def test_trace_core_reads_it():
    src = (ROOT / "vera" / "dag" / "loop_trace_core.py").read_text(encoding="utf-8")
    assert 'plan["tier_lower_applied"] = bool(e.get("tier_lower_applied"))' in src
