"""A test run that reports failures is a finding, not a broken command.

build-multifile is the one census goal that got WORSE while everything else
improved:

    census   wall   steps   tool calls   warnings
    run59    1003    4/5        24           9
    run60     611    4/4        10           4
    run61    1411    5/7        27          11
    run62    1762    4/6        34          12

Every long run carries the same line - `exec.bash.run FAILED - ==============
test session starts ==============`. pytest exits non-zero when tests fail,
which is how it reports the answer, so the loop read a perfectly good test run
as a broken call: the step retried the command 3-7 times, then the controller
INSERTED a step, or failure-recovery REPLACED one, or the completion gate
APPENDED "Run pytest to verify implementation" - each new step costing another
executor call and its embeddings. The embed count was a symptom of the step
count, not a cause.

The mirror image matters too, and is in the original evidence for this goal:
`python -m unittest` reporting "Ran 0 tests ... OK" exits ZERO, so the loop read
it as a pass when nothing had been verified.

Real runner output, pasted verbatim. Pure: no Redis, no app import.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.execution import exec_result_note as N        # noqa: E402

PYTEST_FAIL = {"rc": 1, "stderr": "", "stdout": """\
============================= test session starts ==============================
platform linux -- Python 3.11.2, pytest-9.0.2, pluggy-1.6.0
collected 8 items

tests/test_stats.py ..F..F..                                             [100%]

=================================== FAILURES ===================================
_________________________________ test_median __________________________________
E   AssertionError: assert 3 == 2.5
=========================== short test summary info ============================
FAILED tests/test_stats.py::test_median - AssertionError: assert 3 == 2.5
========================= 2 failed, 6 passed in 0.42s ==========================
"""}

PYTEST_PASS = {"rc": 0, "stderr": "", "stdout": """\
============================= test session starts ==============================
collected 12 items

tests/test_stats.py ............                                         [100%]

============================== 12 passed in 1.02s ==============================
"""}

PYTEST_NOTHING = {"rc": 5, "stderr": "", "stdout": """\
============================= test session starts ==============================
collected 0 items

============================ no tests ran in 0.01s =============================
"""}

UNITTEST_ZERO = {"rc": 0, "stdout": "", "stderr": "Ran 0 tests in 0.000s\n\nOK\n"}
UNITTEST_FAIL = {"rc": 1, "stdout": "",
                 "stderr": "Ran 5 tests in 0.003s\n\nFAILED (failures=2, errors=1)\n"}

NOT_A_TEST = {"rc": 1, "stdout": "", "stderr": "bash: statkit: command not found\n"}
SILENT_OK = {"rc": 0, "stdout": "", "stderr": ""}


# -- the census case ---------------------------------------------------------
def test_a_failing_pytest_run_is_reported_as_a_result():
    assert N.is_test_failure(PYTEST_FAIL)
    note = N.annotate(PYTEST_FAIL, command="python -m pytest -q")["note"]
    assert "RAN and reported 2 failed, 6 passed" in note
    assert "Re-running it unchanged returns the same thing" in note
    # and it says what to do instead of retrying
    assert "fix the code or the test" in note


def test_the_counts_come_from_the_runner_not_from_guessing():
    s = N.test_run_summary(PYTEST_FAIL)
    assert s == {"ran": True, "counts": {"failed": 2, "passed": 6},
                 "text": "2 failed, 6 passed"}


def test_a_failing_unittest_run_is_reported_too():
    assert N.is_test_failure(UNITTEST_FAIL)
    assert "FAILED (failures=2, errors=1)" in N.annotate(UNITTEST_FAIL)["note"]


# -- nothing ran -------------------------------------------------------------
def test_ran_0_tests_is_not_a_pass():
    """unittest exits ZERO here, so this looked like success and verified
    nothing - it is in the original evidence for this goal."""
    assert N.found_no_tests(UNITTEST_ZERO)
    assert not N.is_test_failure(UNITTEST_ZERO)
    note = N.annotate(UNITTEST_ZERO)["note"]
    assert "found NO TESTS" in note and "working directory" in note


def test_pytest_collecting_nothing_is_the_same_answer():
    assert N.found_no_tests(PYTEST_NOTHING)
    assert "found NO TESTS" in N.annotate(PYTEST_NOTHING)["note"]


# -- what must NOT be annotated ----------------------------------------------
def test_a_passing_run_gets_no_note():
    """It exited 0 and printed a summary; there is nothing to explain."""
    assert N.test_run_summary(PYTEST_PASS)["ran"] is True
    assert not N.is_test_failure(PYTEST_PASS)
    assert "note" not in N.annotate(PYTEST_PASS)


def test_an_ordinary_failure_is_still_an_ordinary_failure():
    assert N.test_run_summary(NOT_A_TEST) is None
    assert not N.is_test_failure(NOT_A_TEST)
    assert "note" not in N.annotate(NOT_A_TEST)


def test_the_silent_notes_are_unchanged():
    """The two existing notes must keep firing - this adds a case, it does not
    take one over."""
    assert N.annotate(SILENT_OK)["note"] == N.NOTE
    assert "exited 1 and printed NOTHING" in N.annotate(
        {"rc": 1, "stdout": "", "stderr": ""})["note"]


def test_a_caller_that_already_explained_itself_wins():
    res = dict(PYTEST_FAIL, note="something the caller knows")
    assert N.annotate(res)["note"] == "something the caller knows"


def test_a_non_exec_result_is_never_touched():
    for res in ([], "text", None, {"ok": True}, {"stdout": "2 failed"}):
        assert N.test_run_summary(res) is None
        assert N.annotate(res) is res


def test_the_word_pytest_alone_is_not_a_test_run():
    """Conservative on purpose: only a runner's own summary counts."""
    assert N.test_run_summary(
        {"rc": 0, "stdout": "installing pytest\n", "stderr": ""}) is None
    assert N.test_run_summary(
        {"rc": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'pytest'\n"}
    ) is None
