"""Redundant-verify plan prune (vera/dag/plan_guards.py).

Every "should drop" case below is a step a REAL v7 run actually planned on
2026-08-24; every "must keep" case is either a real step that was legitimate, or
the false positive that would do the most damage if the rule were sloppy.

Pure (no I/O, no app import), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag.plan_guards import (  # noqa: E402
    is_redundant_verify_step, prune_redundant_verify_steps)


AUTHOR = {"id": 1, "title": "Create habit.html with UI, logic, and persistence",
          "caps": ["code.author"], "goal": "author habit.html", "success": "habit.html exists"}


def _drops(step, prior=(AUTHOR,)):
    return is_redundant_verify_step(step, list(prior))


# ── real steps that SHOULD be dropped ────────────────────────────────────────
def test_verify_syntax_and_completeness():
    assert _drops({"title": "Verify JavaScript syntax and completeness",
                   "caps": ["code.author", "ide.fs.read", "exec.python.run"],
                   "goal": "check the embedded JS is valid",
                   "success": "the file contains valid HTML5 syntax"})


def test_verify_syntax_by_reading_the_file():
    assert _drops({"title": "Verify JavaScript syntax by reading the file",
                   "caps": ["code.author", "ide.fs.read"], "goal": "", "success": ""})


def test_confirm_final_deliverable_state():
    assert _drops({"title": "Confirm final deliverable state",
                   "caps": ["ide.fs.read"], "goal": "confirm habit.html exists",
                   "success": "the deliverable exists"})


def test_real_success_criteria_from_sampled_runs():
    """The three `success` texts real plans actually produced for that step.

    The middle one caught a bug in a first version of this rule: it matched a
    bare "render", so "functions for rendering/updating" — a description of the
    file's CONTENTS — was mistaken for a behavioural test and kept.
    """
    for success in (
        "Python parser reports 'syntax_ok' is true for the extracted JS code.",
        "The file content includes a complete <script> block with no obvious truncation "
        "or missing braces, covering data structures for habits/dates and functions for "
        "rendering/updating.",
        "'habit.html' has been read successfully, confirming the <script> block contains "
        "a complete implementation of the habit tracker logic.",
    ):
        assert _drops({"title": "Verify JavaScript syntax and completeness",
                       "caps": ["code.author", "ide.fs.read"],
                       "goal": "", "success": success}), success


# ── steps that MUST be kept ──────────────────────────────────────────────────
def test_behaviour_check_via_operator_is_kept():
    """The most damaging false positive: real behaviour verification."""
    assert not _drops({"title": "Exercise behavior by loading in browser",
                       "caps": ["operator.run"], "goal": "click add-habit and confirm it persists",
                       "success": "a habit survives reload"})


def test_syntax_AND_runtime_behaviour_is_kept():
    """Mentioning behaviour is enough to keep it — we err towards keeping."""
    assert not _drops({"title": "Verify JavaScript syntax and runtime behavior",
                       "caps": ["code.author", "exec.bash.run"], "goal": "", "success": ""})


def test_step_with_no_prior_author_is_kept():
    """Nothing was authored, so the check is not redundant."""
    assert not is_redundant_verify_step(
        {"title": "Verify the config file syntax", "caps": ["ide.fs.read"],
         "goal": "", "success": ""},
        [{"id": 1, "title": "Fetch config", "caps": ["http.get"]}])


def test_authoring_step_itself_is_kept():
    assert not _drops(AUTHOR)


def test_step_that_does_real_work_is_kept():
    assert not _drops({"title": "Add a dark mode toggle", "caps": ["code.edit"],
                       "goal": "add a toggle", "success": "toggle present"})


def test_verify_without_form_words_is_kept():
    """"Verify the results" is too vague to be sure it is redundant — keep it."""
    assert not _drops({"title": "Verify the results", "caps": ["ide.fs.read"],
                       "goal": "", "success": ""})


def test_unknown_cap_keeps_the_step():
    """A cap we cannot classify might observe behaviour — never prune blind."""
    assert not _drops({"title": "Verify syntax is valid", "caps": ["some.new.cap"],
                       "goal": "", "success": ""})


# ── prune(): renumbering, needs-repair, and the never-empty rule ─────────────
def test_prune_renumbers_and_repairs_needs():
    plan = [
        dict(AUTHOR),
        {"id": 2, "title": "Verify JavaScript syntax by reading the file",
         "caps": ["ide.fs.read"], "goal": "", "success": "", "needs": [1]},
        {"id": 3, "title": "Exercise behavior in browser", "caps": ["operator.run"],
         "goal": "", "success": "", "needs": [1, 2]},
    ]
    res = prune_redundant_verify_steps(plan)
    assert [s["title"] for s in res["steps"]] == [AUTHOR["title"], "Exercise behavior in browser"]
    assert [s["id"] for s in res["steps"]] == [1, 2]
    assert res["steps"][1]["needs"] == [1]          # the dropped step's ref is gone
    assert len(res["dropped"]) == 1


def test_prune_never_empties_a_plan():
    only = [{"id": 1, "title": "Verify syntax is valid", "caps": ["ide.fs.read"],
             "goal": "", "success": ""}]
    # no prior author anyway, but assert the invariant explicitly
    res = prune_redundant_verify_steps(only)
    assert len(res["steps"]) == 1


def test_prune_leaves_a_clean_plan_untouched():
    plan = [dict(AUTHOR)]
    res = prune_redundant_verify_steps(plan)
    assert res["dropped"] == []
    assert len(res["steps"]) == 1


def test_prune_handles_empty_and_garbage():
    assert prune_redundant_verify_steps([])["steps"] == []
    assert is_redundant_verify_step(None, [AUTHOR]) is False
