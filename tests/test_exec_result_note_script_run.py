"""Eleven identical tracebacks from running a test file as a script.

Census run67, build-multifile: `exec.python.run FAILED` eleven times, every one

    Traceback (most recent call last):
      File "/workspace/statkit/test_stats.py", line 63, in <module>
        main()
      ...

The model ran `python test_stats.py`. That does not run tests; it runs the
module's top level - a `main()` it had written into the file - and the
traceback says nothing about the CALL being the wrong shape, so the model
varied the command and tried again. The runner notes (`test_run_summary`)
cannot help: there is no runner and so no summary.

Pure: no Redis, no app import.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.execution import exec_result_note as N        # noqa: E402

TB = ("Traceback (most recent call last):\n"
      "  File \"/workspace/statkit/test_stats.py\", line 63, in <module>\n"
      "    main()\n"
      "  File \"/workspace/statkit/test_stats.py\", line 46, in main\n"
      "    assert stats.mean([1, 2]) == 2\n"
      "AssertionError\n")
FAIL = {"rc": 1, "stdout": "", "stderr": TB}


def test_the_census_case_is_named():
    res = N.annotate(FAIL, command="python test_stats.py")
    assert N.script_run_of_test_file(res) == "test_stats.py"
    note = res["note"]
    assert "ran the test file `test_stats.py` directly as a script" in note
    assert "python -m pytest -q test_stats.py" in note
    assert "Re-running it as a script will raise the same thing" in note


def test_paths_and_interpreter_spellings():
    for cmd, name in (("python3 ./statkit/test_stats.py", "./statkit/test_stats.py"),
                      ("cd /workspace && python -u tests/test_api.py", "tests/test_api.py"),
                      ("python3.11 test_x.py --verbose", "test_x.py")):
        assert N.script_run_of_test_file(dict(FAIL, command=cmd)) == name, cmd


def test_a_runner_is_not_a_script_run():
    """`python -m pytest test_x.py` is the RIGHT call; its failures are the
    runner's to explain (test_run_summary), never this note."""
    for cmd in ("python -m pytest -q test_stats.py", "pytest test_stats.py",
                "python -m unittest test_stats", "python -m test_stats"):
        assert N.script_run_of_test_file(dict(FAIL, command=cmd)) == "", cmd
    pyt = {"rc": 1, "stderr": "", "stdout":
           "=== test session starts ===\ncollected 1 item\n\ntest_stats.py F\n"
           "=== 1 failed in 0.1s ===\n"}
    assert "RAN and reported" in N.annotate(pyt, command="python -m pytest -q test_stats.py")["note"]


def test_only_a_failed_traceback_qualifies():
    ok = {"rc": 0, "stdout": "all good\n", "stderr": ""}
    assert N.script_run_of_test_file(dict(ok, command="python test_stats.py")) == ""
    assert "note" not in N.annotate(ok, command="python test_stats.py")
    no_tb = {"rc": 2, "stdout": "", "stderr": "usage: test_stats.py [-h]\n"}
    assert N.script_run_of_test_file(dict(no_tb, command="python test_stats.py")) == ""
    assert N.script_run_of_test_file(dict(FAIL, command="python stats.py")) == ""


def test_the_other_notes_and_the_caller_still_win():
    assert N.annotate({"rc": 0, "stdout": "", "stderr": ""})["note"] == N.NOTE
    keep = dict(FAIL, note="the caller explained it")
    assert N.annotate(keep, command="python test_stats.py")["note"] == "the caller explained it"
    for res in ([], "x", None, {"ok": False}):
        assert N.script_run_of_test_file(res) == ""
