"""A run that names a file that is not there must be told what is.

Census 36, research-web, step 6 - the literal cycle list:

    exec.python.run  python3: can't open file '/workspace/inspect_and_extract_browsers.py'
    exec.python.run  python3: can't open file '/workspace/inspect_and_extract_browsers.py'
    code.edit        132s on that same missing file, no changes made
    exec.python.run  python3: can't open file '/workspace/extract_browser_support_details_fixed.py'

/workspace held exactly one script the whole time:

    extract_browser_support_details_from_mdn.py

Four cycles and ~135s of tool time spent on two filenames that never existed,
in a run that was cancelled at its 30-minute wall cap without ever reaching the
step that writes the summary.

Pure: the listing is passed in, so there is no container and no I/O here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.execution import missing_path_hint as MP      # noqa: E402

REAL = "extract_browser_support_details_from_mdn.py"
WORKSPACE = [REAL, "journal.json"]

PY_MISSING = ("python3: can't open file '/workspace/inspect_and_extract_browsers.py': "
              "[Errno 2] No such file or directory")
NEAR_MISS = ("python3: can't open file '/workspace/extract_browser_support_details_fixed.py': "
             "[Errno 2] No such file or directory")


def _fail(stderr):
    return {"ok": False, "rc": 2, "stdout": "", "stderr": stderr}


# ── reading the failure ─────────────────────────────────────────────────────
def test_the_missing_path_is_extracted_from_the_interpreter_message():
    assert MP.missing_path(PY_MISSING) == "/workspace/inspect_and_extract_browsers.py"


def test_a_plain_errno_message_also_yields_its_path():
    assert MP.missing_path("FileNotFoundError: [Errno 2] No such file or "
                           "directory: '/workspace/report.md'") == "/workspace/report.md"


def test_an_unrelated_failure_is_not_treated_as_a_missing_path():
    """A traceback that merely quotes something must not be rewritten."""
    assert MP.missing_path("ZeroDivisionError: division by zero") == ""
    assert MP.is_missing_path_failure(_fail("Traceback: KeyError: 'name'")) is False


def test_a_successful_run_is_never_touched():
    ok = {"ok": True, "rc": 0, "stdout": "done", "stderr": ""}
    assert MP.is_missing_path_failure(ok) is False
    assert MP.augment(dict(ok), WORKSPACE) == ok


def test_a_run_that_SUCCEEDS_while_printing_that_message_is_left_alone():
    """The false positive that matters, and the one an earlier version of this
    test could not see: plenty of commands exit 0 while their output mentions a
    missing file - a grep over a stale list, a script logging a skipped path.
    Rewriting a SUCCESS to say "does not exist. The working directory contains"
    would invent a failure that did not happen."""
    ok = {"ok": True, "rc": 0,
          "stdout": "checked 3 paths\n/workspace/old.py: No such file or directory",
          "stderr": ""}
    assert MP.is_missing_path_failure(ok) is False
    assert MP.augment(dict(ok), WORKSPACE) == ok


# ── what the model is told ──────────────────────────────────────────────────
def test_the_hint_names_the_files_that_exist():
    h = MP.hint("/workspace/inspect_and_extract_browsers.py", WORKSPACE)
    assert REAL in h and "journal.json" in h


def test_the_near_miss_is_called_out_by_name():
    """..._fixed.py vs ..._from_mdn.py - one word apart. Leaving that to be
    spotted in a list is how the third failed cycle happened."""
    h = MP.hint("/workspace/extract_browser_support_details_fixed.py", WORKSPACE)
    assert "Did you mean %s?" % REAL in h


def test_an_unrelated_name_gets_the_listing_but_no_false_suggestion():
    h = MP.hint("/workspace/zzzzzz.py", ["alpha.md", "beta.csv"])
    assert "alpha.md" in h and "Did you mean" not in h


def test_the_model_is_told_not_to_retry_the_same_path():
    """Two of the four cycles were the IDENTICAL path run twice."""
    h = MP.hint("/workspace/x.py", WORKSPACE)
    assert "do not retry the same path" in h


# ── the honesty rules ───────────────────────────────────────────────────────
def test_an_undeterminable_listing_says_nothing():
    """artifact_list_files returns None when it could not read the directory.
    Claiming "the directory contains: " off a failed probe would be a lie."""
    assert MP.hint("/workspace/x.py", None) == ""
    r = MP.augment(_fail(PY_MISSING), None)
    assert "contains" not in str(r.get("stderr"))


def test_an_empty_directory_is_stated_as_a_fact():
    """[] is different from None: the directory really is empty."""
    h = MP.hint("/workspace/x.py", [])
    assert "empty" in h and "contains:" not in h


# ── the result the loop sees ────────────────────────────────────────────────
def test_the_hint_lands_on_stderr_where_the_loop_reads_it():
    """result_failure_reason reads stderr for an exec failure, so a hint that
    is not there is a hint the model never sees."""
    r = MP.augment(_fail(PY_MISSING), WORKSPACE)
    assert REAL in r["stderr"]
    assert "can't open file" in r["stderr"], "the original message is kept"


def test_the_original_failure_fields_survive():
    r = MP.augment(_fail(PY_MISSING), WORKSPACE)
    assert r["ok"] is False and r["rc"] == 2


def test_the_missing_path_is_reported_as_a_field():
    r = MP.augment(_fail(NEAR_MISS), WORKSPACE)
    assert r["missing_path"] == "/workspace/extract_browser_support_details_fixed.py"
    assert REAL in r["missing_path_hint"]


def test_a_failure_carrying_only_error_is_augmented_there():
    r = MP.augment({"ok": False, "error": PY_MISSING}, WORKSPACE)
    assert REAL in r["error"]


def test_a_non_dict_result_is_returned_unchanged():
    assert MP.augment("boom", WORKSPACE) == "boom"


def test_workspace_names_passes_none_through():
    """None must stay None all the way to hint(), or the honesty rule above
    is bypassed before it can apply."""
    assert MP.workspace_names(None) is None
    assert MP.workspace_names(["a", "b"]) == ["a", "b"]
