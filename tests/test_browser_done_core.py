"""A browser result that says done closes the step's browser work (plan item 24a).

run70-73 (24 Sep 2026): after an operator.run came back {"done": true} the
executor re-issued the browser in every browser goal - four refused turns in a
row in run71, and real second runs of 234 s and 552 s in run73.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.operator import browser_done_core as B  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
DONE = {"ok": True, "done": True, "reason": "done",
        "summary": "Goal complete - Page loaded, email field e1 was clicked, 'not-an-email' typed, "
                   "Tab pressed, and the inline error 'Invalid email format' appeared."}


def test_a_done_result_yields_its_summary():
    assert B.done_summary(DONE) == DONE["summary"]
    assert B.done_summary({"ok": True, "done": True}) == "done"


def test_anything_else_yields_nothing():
    assert B.done_summary({"ok": False, "done": True, "summary": "x"}) == ""
    assert B.done_summary({"ok": False, "error": "time_budget: stopped after 504s"}) == ""
    assert B.done_summary({"ok": True, "done": False, "summary": "still going"}) == ""
    assert B.done_summary("ERROR: no_progress") == "" and B.done_summary(None) == ""


def test_the_first_reissue_is_served_and_the_second_ends_the_step():
    assert not B.should_end(1)
    assert B.should_end(2) and B.should_end(5)
    assert not B.should_end("nonsense")


def test_the_note_carries_the_browsers_report_and_names_the_way_out():
    note = B.describe("operator.run", DONE["summary"])
    assert "operator.run" in note and "ALREADY answered" in note
    assert "Invalid email format" in note and "emit `done`" in note and "edit the file" in note


def test_the_executor_records_the_done_result_and_serves_it_before_the_time_budget():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "browser_done[str(step_id)] = _dsum" in src
    assert 'browser_done_core as _browser_done' in src
    served = src.index("str(step_id) in browser_done)")
    budget = src.index("_step_budget.exhausted(\n                browser_seconds, step_id, tool)")
    assert served < budget
    assert "_browser_done.should_end(browser_done_served[str(step_id)])" in src
    assert "pending_note = _browser_done.describe(tool, _dsum)" in src
