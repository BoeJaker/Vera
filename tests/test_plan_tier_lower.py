"""plan.tier_lower (four-minds FM-4.3, compute-roles lands the floor): a goal the deterministic floor
calls 'complex' only because of its conjunction count falls to the length rules when the switch is
on. Measured 11 Oct: 73% of finished complex-tier census runs executed <= 2 steps."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
SB = (ROOT / "vera" / "minds" / "switchboard_core.py").read_text(encoding="utf-8")

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

pytestmark = pytest.mark.critical

CONJ_ONLY = "write stats.py, add a test file, run the tests, and print the result"


@needs_app
def test_conjunction_only_complex_falls_to_simple_when_lowered():
    assert M._v7_tier_heuristic(CONJ_ONLY) == "complex"
    assert M._v7_tier_heuristic(CONJ_ONLY, lower=True) == "simple"


@needs_app
def test_hints_and_length_still_make_complex_when_lowered():
    hint = next(iter(M._V7_COMPLEX_HINTS))
    g = "please %s for the project" % hint
    assert M._v7_tier_heuristic(g, lower=True) == M._v7_tier_heuristic(g)
    long_goal = " ".join(["word"] * 61)
    assert M._v7_tier_heuristic(long_goal, lower=True) == "complex"
    assert M._v7_tier_heuristic("hello", lower=True) == M._v7_tier_heuristic("hello")


def test_switch_registered_off_by_default_and_read_by_the_tier_decision():
    assert '_f("plan.tier_lower", ("off", "on"), "loop", "FM-4.3",' in SB
    d = SRC[SRC.index("async def _v7_decide_tier("):]
    d = d[:d.index("\n# ═")]
    assert '_minds_core.mode_of("plan.tier_lower") == "on"' in d
    assert "heur = _v7_tier_heuristic(goal, lower=_lower)" in d and '"tier_lower": _lower' in d
