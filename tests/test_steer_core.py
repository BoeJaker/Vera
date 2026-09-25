"""The controller's steer stays out of authoring tasks (plan item 20).

run70-73 (24 Sep 2026): six code.author/code.edit tasks contained the
"CONTROLLER STEER (after step N, ...)" block the controller had appended to
the step goal, and the author wrote scripts whose purpose was the steer.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.steer_core import has_steer, strip_steer  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
STEERED = ("Extract the total size string and parse the top-5 list from the command output to format it."
           "\n\nCONTROLLER STEER (after step 1, alignment: advances): Parse the raw shell output into a "
           "structured report containing total disk usage and the list of five largest files.")


def test_the_census_case_is_stripped_to_the_task():
    assert has_steer(STEERED)
    assert strip_steer(STEERED) == ("Extract the total size string and parse the top-5 list from the "
                                    "command output to format it.")


def test_text_without_a_steer_is_unchanged():
    assert not has_steer("Create clock.html with a live clock")
    assert strip_steer("Create clock.html with a live clock") == "Create clock.html with a live clock"
    assert strip_steer(None) == "" and not has_steer("")


def test_a_steer_with_a_different_alignment_word_still_strips():
    t = "Do the thing\n\nCONTROLLER STEER (after step 3, alignment: off_track): stop and report"
    assert strip_steer(t) == "Do the thing"


def test_the_loop_strips_at_the_heal_the_fallback_and_dispatch():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert 'sg = _steer_core.strip_steer(str(step.get("goal") or step.get("title") or "")).strip()' in src
    assert "return _steer_core.strip_steer(goal)[:300]" in src
    assert 'note": f"{_sf}: controller steer removed (it is context, not the task)"' in src
    ast.parse(src)
