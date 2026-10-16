"""Completion gate: a claim naming the SAME file as the goal is grounded, however it is written;
an incomplete verdict with nothing to run gets one step from its own missing list (16 Oct 2026).
The claim strings are the ones the live gate dropped (prod logs, 16 Oct). Imports the monolith
module, so it runs in-container (see test_loop_cancel.py)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402

pytestmark = pytest.mark.critical

STATKIT = ("Create a small Python package at /workspace/statkit with stats.py providing mean, "
           "median and mode, plus a test file, then run the tests.")
HELLO = ("Create /workspace/hello.html containing the exact text VERA-OPERATOR-OK, then open it in "
         "a browser and report the text you can see on the rendered page.")


@pytest.mark.parametrize("claim", ["statkit/stats.py", "statkit/test_stats.py",
                                   "test_stats.py inside /workspace/statkit/",
                                   "/workspace/statkit/stats.py does not define mean"])
def test_statkit_gaps_are_real(claim):
    kept, dropped = W._v6_ground_missing_claims([claim], STATKIT)
    assert kept == [claim] and dropped == []


def test_operator_follow_up_to_the_served_file_is_grounded():
    # the logged text is cut at 200 chars; the path that got it dropped was in the cut-off
    # tail, so these are the forms such a step writes the served file in
    goal_files = set(W._v6_extract_paths(HELLO))
    for text in ("open localhost:9001/hello.html in operator.run",
                 "serve /workspace/serve/hello.html and open it",
                 "operator.run on ./hello.html, report the visible text"):
        claimed = W._v6_extract_paths(text)
        assert claimed and W._v6_claim_grounded(claimed, goal_files), (text, claimed)


def test_invented_files_are_still_dropped():
    kept, dropped = W._v6_ground_missing_claims(
        ["'toggle.html' - named in the goal, but does not exist"], "Make lightswitch.html with a toggle")
    assert kept == [] and len(dropped) == 1
    kept, dropped = W._v6_ground_missing_claims(["__init__.py (if needed for package import)"], STATKIT)
    assert dropped and not kept
    # a file in a folder the goal never names
    assert not W._v6_claim_grounded(["other/stats_extra.py"], set(W._v6_extract_paths(STATKIT)))


def test_claims_without_a_file_are_untouched():
    kept, dropped = W._v6_ground_missing_claims(["the report has no citations"], "Write a report")
    assert kept == ["the report has no citations"] and dropped == []


def test_incomplete_gate_with_nothing_to_run_gets_one_fix_step():
    gate = {"complete": False, "missing": ["Browser opened and text rendered on page reported"], "follow_up": []}
    assert W._v6_needs_missing_fix(gate, [], steps_left=5, already=False)
    assert not W._v6_needs_missing_fix(gate, [], steps_left=5, already=True)     # once per run
    assert not W._v6_needs_missing_fix(gate, [], steps_left=0, already=False)    # no budget
    assert not W._v6_needs_missing_fix(gate, [{"id": 9}], steps_left=5, already=False)
    assert not W._v6_needs_missing_fix(dict(gate, bounded=True), [], steps_left=5, already=False)
    assert not W._v6_needs_missing_fix({"complete": True, "missing": []}, [], 5, False)
    assert not W._v6_needs_missing_fix({"complete": False, "missing": [" "]}, [], 5, False)
    step = W._v6_missing_fix_step(gate["missing"], HELLO)
    assert "Browser opened" in step["goal"] and HELLO in step["goal"] and step["success"]


def test_gate_loop_uses_the_fix():
    src = open(W.__file__, encoding="utf-8").read()
    i = src.index("_v6_needs_missing_fix(gate, follow_up")
    assert src.index("if not follow_up:", i) - i < 900
