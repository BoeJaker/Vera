"""A step that asks for a from-scratch rewrite gets one, even of a file that once ran
(census 2026-09-30 analyse-data)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import rewrite_intent_core as R  # noqa: E402

ASKS = [
    "Re-author complete analysis script from scratch",
    "The previous attempt failed because incremental edits corrupted the file. Rewrite it.",
    "Start over with a fresh script that parses the single CSV line",
    "Replace the whole file with a correct implementation",
    "reauthoring analyze_stats.py",
]
NOT = [
    "Fix the syntax error in analyze_stats.py",
    "Create hello.html showing Hello, world",
    "Add 'import math' to the imports section",
    "Change the countdown to 90 seconds leaving everything else identical",
    "Rewrite only the reset() function",
    "",
]


def test_asks_for_a_rewrite():
    for t in ASKS:
        assert R.rewrite_asked(t), t


def test_ordinary_steps_do_not():
    for t in NOT:
        assert not R.rewrite_asked(t), t


def test_the_router_lets_an_asked_rewrite_through_and_sees_the_title():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    rule1 = src[src.index("# RULE 1"):src.index("# RULE 2")]
    assert "_rewrite_intent_core.rewrite_asked(step_goal)" in rule1
    assert src.count("step_goal=_v5_step_text_for_route(step, goal)") == 2
