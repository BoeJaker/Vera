"""Completion gate: actions the goal asks for are checked against the calls the run made (16-18 Oct 2026),
and a test-file claim is grounded when the goal asks for tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import gate_actions_core as ga  # noqa: E402

pytestmark = pytest.mark.critical

STATKIT = ("Create a small Python package at /workspace/statkit with stats.py providing mean, median and "
           "mode, plus a test file, then run the tests.")
HELLO = ("Create /workspace/hello.html containing the exact text VERA-OPERATOR-OK, then open it in a browser "
         "and report the text you can see on the rendered page.")
TIMER = "Make a timer page called timer.html."


def _res(*calls):
    return [{"history": [{"tool": t, "args": a} for t, a in calls]}]


def test_required_actions_from_the_goal():
    assert ga.required(STATKIT) == ["run_tests"]
    assert ga.required(HELLO) == ["browser"]
    assert ga.required(TIMER) == []
    assert ga.required("Write a report on X with citations.") == []
    assert "browser" in ga.required("Build the page and verify it in a real browser.")


def test_tests_must_actually_be_run():
    assert [m["action"] for m in ga.missing(STATKIT, _res(("code.author", {"path": "stats.py"})))] == ["run_tests"]
    assert ga.missing(STATKIT, _res(("exec.bash.run", {"command": "cd /workspace/statkit && python -m pytest -q"}))) == []
    assert ga.missing(STATKIT, _res(("evolve.unittest.run", {"branch": "x"}))) == []
    # running something that is not a test does not count
    assert ga.missing(STATKIT, _res(("exec.bash.run", {"command": "python stats.py"})))


def test_curl_is_not_a_browser():
    miss = ga.missing(HELLO, _res(("exec.bash.run", {"command": "python -m http.server 9001"}),
                                  ("exec.bash.run", {"command": "curl localhost:9001/hello.html"})))
    assert [m["action"] for m in miss] == ["browser"] and miss[0]["step"]["caps"] == ["operator.run"]
    assert ga.missing(HELLO, _res(("operator.run", {"goal": "open hello.html"}))) == []


def test_test_file_claim_is_grounded_when_the_goal_asks_for_tests():
    from vera.dag import dag_workshop_capabilities as W
    kept, dropped = W._v6_ground_missing_claims(["test file (e.g. test_stats.py)"], STATKIT)
    assert kept and not dropped
    kept, dropped = W._v6_ground_missing_claims(["test_stats.py"], "Make lightswitch.html with a toggle")
    assert dropped and not kept                       # no tests asked for: still invented


def test_gate_prompt_no_longer_lets_the_register_settle_content():
    from vera.dag import dag_workshop_capabilities as W
    src = open(W.__file__, encoding="utf-8").read()
    assert "does NOT settle what the goal asks the file to CONTAIN" in src
    assert "_gate_actions.missing(_grounding_goal, results)" in src
