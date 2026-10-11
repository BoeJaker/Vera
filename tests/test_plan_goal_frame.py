"""plan.goal_frame (compute-roles B2 / four-minds FM-1.3, agreed 11 Oct 2026): the goal's dates,
versions and numeric limits become done_when items and the final gate's must-cover list, and the
run records which ones its answer met. Off by default."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import goal_constraints_core as C  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
SB = (ROOT / "vera" / "minds" / "switchboard_core.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


def test_extracts_dates_versions_and_limits():
    g = ("Write a report on Valkey since March 2024 for Python 3.12 users, in under 500 words, "
         "listing the top 5 changes made in 2025.")
    cons = C.extract(g)
    kinds = [(c["kind"], c["token"]) for c in cons]
    assert ("date", "2024") in kinds and ("date", "2025") in kinds
    assert ("version", "3.12") in kinds
    assert ("word_limit", "500") in kinds and ("count", "5") in kinds
    assert C.extract("write hello.html with a heading") == []
    assert len(C.extract(" ".join("in %d" % (2000 + i) for i in range(20)))) == C.MAX_ITEMS


def test_block_and_coverage():
    cons = C.extract("Summarise news since 2024 in under 10 words for Python 3.12")
    assert C.block(cons).startswith("GOAL CONSTRAINTS") and C.block([]) == ""
    cov = C.coverage(cons, "Python 3.12 shipped; 2024 was busy.")
    assert cov["missing"] == [] and cov["ratio"] == 1.0
    cov = C.coverage(cons, "A long answer " * 10)
    assert "since 2024" in cov["missing"] and any("under 10 words" == m for m in cov["missing"])


def test_wired_behind_the_switch():
    assert '_f("plan.goal_frame", ("off", "on"), "loop", "FM-1.3",' in SB
    assert 'mode_of("plan.goal_frame") == "on"' in SRC
    i = SRC.index("    if _goal_frame_on():\n        _goal_cons = _goal_cons_core.extract(goal)")
    assert i < SRC.index('    steps = plan.get("steps") or []\n    if not steps:', i)
    assert "_gate_goal = _gate_goal + \"\\n\\n\" + _goal_cons_core.block(_goal_cons)" in SRC
    assert '"agent_loop_v6.goal_constraint_coverage"' in SRC
