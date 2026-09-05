"""An edit whose work has already been done was reported as a failure.

Census 34, author-then-edit ("create timer.html with a 60 second countdown,
then change the countdown to 90"):

    code.edit FAILED - edit 1: `find` text not present in the file
    (first 60 chars: 'let countdown = 60;').
    Closest text actually in the file - line 35: 'let countdown = 90;'

The editor was shown the CURRENT file - _v5_load_current reads the versioned
store first, so it saw the 90. What nothing told it was that the task it had
been handed was already satisfied, so it anchored on the text the task named
and that anchor was gone, removed by its own earlier edit. code.edit then ran
four times against a file that was already correct and the goal wall-capped.

`find` absent AND `replace` present is what a finished edit looks like from
the outside, and it is objective - no judgement, no model.

Pure: no I/O.
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vera.dag.edit_already_applied import (already_applied,   # noqa: E402
                                           describe, partition)

pytestmark = pytest.mark.critical

FIND = "let countdown = 60;"
REPL = "let countdown = 90;"
FILE = "var x = 1;\nlet countdown = 90;\nfunction tick() {}\n"


def _wt(name):
    """This branch's module - Vera.vera.dag resolves to the MAIN checkout."""
    spec = importlib.util.spec_from_file_location(
        "_wt_" + name, os.path.join(ROOT, "vera", "dag", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── the census case ────────────────────────────────────────────────────────

def test_the_census34_edit_is_recognised_as_already_done():
    assert already_applied(FILE, FIND, REPL) is True


def test_a_genuinely_missing_anchor_is_not_called_done():
    """The common case must keep failing - this is the difference between
    'you are editing the wrong file' and 'this is finished'."""
    assert already_applied("nothing like it here", FIND, REPL) is False


# ── the guards that keep it narrow ─────────────────────────────────────────

def test_a_deletion_is_never_called_already_applied():
    """An empty replacement is present in every file by definition."""
    assert already_applied(FILE, FIND, "") is False


def test_a_short_replacement_proves_nothing():
    """'}' or 'true' occurs everywhere by accident, and treating a coincidence
    as completion would silently SKIP a real edit."""
    assert already_applied("if (x) { }\n", "if (y) { }", "}") is False
    assert already_applied("a = true\n", "a = false", "true") is False


def test_a_replace_identical_to_the_find_says_nothing():
    assert already_applied(FILE, REPL, REPL) is False


def test_both_conditions_are_required():
    """Present replace alone is usually just the file containing the text the
    edit would add; absent find alone is usually a wrong anchor."""
    assert already_applied(FILE, "var x = 1;", REPL) is False   # find IS present
    assert already_applied("no ninety here", FIND, REPL) is False  # replace absent


def test_empty_inputs_are_safe():
    assert already_applied("", FIND, REPL) is False
    assert already_applied(FILE, "", REPL) is False
    assert already_applied(FILE, FIND, None) is False


# ── the message ────────────────────────────────────────────────────────────

def test_the_message_says_what_was_found_and_that_nothing_is_wrong():
    m = describe(1, REPL)
    assert "already applied" in m and REPL in m
    assert "Nothing to do" in m


def test_a_multiline_replacement_is_summarised_not_dumped():
    m = describe(2, "line one\nline two\nline three")
    assert "line two" not in m


# ── partitioning a batch ───────────────────────────────────────────────────

def test_a_batch_splits_into_work_and_already_done_keeping_indices():
    todo, done = partition(FILE, [{"find": FIND, "replace": REPL},
                                  {"find": "var x = 1;", "replace": "var x = 2;"}])
    assert [i for i, _ in done] == [1]
    assert [i for i, _ in todo] == [2]


def test_partition_survives_junk_entries():
    todo, done = partition(FILE, [None, "nonsense", {"find": FIND, "replace": REPL}])
    assert len(done) == 1 and todo == []


# ── end to end through the real matcher ────────────────────────────────────

def _apply(content, edits):
    import vera.dag.dag_workshop_capabilities as DW
    real = DW._edit_already
    DW._edit_already = _wt("edit_already_applied")
    try:
        return DW._v5_apply_edits(content, edits)
    finally:
        DW._edit_already = real


def test_the_matcher_succeeds_without_changing_the_file():
    res = _apply(FILE, [{"find": FIND, "replace": REPL}])
    assert res["ok"] is True, res["errors"]
    assert res["errors"] == []
    assert res["content"] == FILE, "an already-applied edit must not rewrite anything"
    assert [a["edit"] for a in res["already_applied"]] == [1]


def test_a_real_miss_still_fails_through_the_matcher():
    res = _apply(FILE, [{"find": "def nowhere():", "replace": "def somewhere_entirely():"}])
    assert res["ok"] is False and res["errors"]


def test_a_mixed_batch_applies_the_outstanding_edit_only():
    res = _apply(FILE, [{"find": FIND, "replace": REPL},
                        {"find": "var x = 1;", "replace": "var x = 42;"}])
    assert res["ok"] is True
    assert "var x = 42;" in res["content"]
    assert len(res["applied"]) == 1 and len(res["already_applied"]) == 1


def test_the_retry_prompt_tells_the_model_not_to_resend_a_done_edit():
    """Left out, the retry is told they are missing and re-derives them -
    which is how a correct file got edited four times."""
    src = open(os.path.join(ROOT, "vera", "dag",
                            "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "DONE ALREADY (do not resend)" in src


def test_the_cap_says_when_nothing_needed_changing():
    """Reported as a plain success, a caller cannot tell that from a no-op and
    a verifier sends the step round again."""
    src = open(os.path.join(ROOT, "vera", "dag",
                            "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "No change was needed" in src
