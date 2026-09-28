"""A command that succeeded and printed nothing has told you something.

Census run 16, build-multifile, step 8. Twelve calls, ten of which SUCCEEDED:

     5 exec.python.run .../tests/test_stats.py     rc=0  stdout=""
     6 python -m py_compile .../test_stats.py      rc=0  stdout=""
     7 (the same again)                            rc=0  stdout=""
     8 python3 -m py_compile ...                   rc=0  stdout=""
     9 python  -m py_compile ...                   rc=0  stdout=""
    10 ls -la ... && python3 -m py_compile ...     rc=0

It looks like thrash and it is not. `py_compile` on a valid file prints nothing;
running a pytest-style file directly prints nothing. So the model was handed
`{"ok": true, "rc": 0, "stdout": "", "stderr": ""}` five times and had no way to
tell whether it had verified anything — an empty string reads as "no
information", not as "the check passed". So it varied the command slightly and
tried again: `python` then `python3`, absolute then relative, `ls &&` prefixed.

The loop's guards were not at fault and were not bypassed. `success_sigs`
de-duplicates identical repeats and `failed_sigs`/`_MAX_REPEAT_FAIL` catch
identical failures — but these calls were neither identical nor failing. Nothing
was broken except the model's ability to conclude.

So the fix is not another guard. It is to stop shipping an ambiguous blank:
silence on success is a RESULT, and saying so costs one sentence.

## Where this is applied

At the loop's result-preview chokepoint (so every capability the model reads
gets it, not just the exec caps someone remembered to patch) AND on the exec
result dicts themselves (so the UI and any direct caller see it too). One
definition, because three hand-rolled copies of a PYTHONPATH prefix drifted in
exactly this file's neighbourhood and cost three days.

Pure: no app imports, no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Keys that mark a dict as an EXEC result rather than some other capability's
# payload. Both must be present: plenty of results carry an `ok`, and annotating
# a non-exec result would be noise at best and misleading at worst.
_RC_KEYS = ("rc", "exit_code", "returncode")
_OUT_KEYS = ("stdout", "stderr")

NOTE = ("the command SUCCEEDED (rc=0) and produced NO output — an empty stdout "
        "here is the result, not a missing one. Do not re-run it to check, and "
        "do not vary it slightly and retry: if you need visible output, ask for "
        "it explicitly (print, ls, echo); if the command's purpose was to "
        "succeed or to verify, it did.")

#: The mirror image, and the reason it needed its own note: `grep`, `test`,
#: `diff` and `pgrep` all exit 1 while printing NOTHING, and for them that is
#: how a NEGATIVE ANSWER is reported - not a fault. Census 41 and 43 produced
#: three of these, and the loop retried. The note deliberately does not tell the
#: model the call succeeded (ok stays False, the exit code is real); it tells it
#: what an empty rc=1 usually MEANS so it stops reading it as a broken call.
FAILURE_NOTE = (
    "the command exited {rc} and printed NOTHING to stdout or stderr. For "
    "grep, test, diff and pgrep that is exactly how they report NO MATCH / NOT "
    "TRUE - so this may be the ANSWER to what you asked, not a broken call. "
    "Re-running it unchanged will return the same thing. If you need the "
    "distinction, ask for it explicitly (`grep -c`, `... || echo NO-MATCH`, or "
    "check the exit code yourself); if you need the command to have produced "
    "output, the problem is upstream of this call.")

#: A test run that EXECUTED and reported failures. The runner exits non-zero
#: when tests fail, which is how it reports the answer - so the loop reads a
#: perfectly good test run as a broken command, retries it, and then the
#: controller inserts a step, failure-recovery replaces one, or the completion
#: gate appends another. build-multifile is the one census goal getting worse
#: while everything else improves, and this is the whole of it:
#:
#:     census   wall   steps   tool calls   warnings
#:     run59    1003    4/5        24          9
#:     run60     611    4/4        10          4
#:     run61    1411    5/7        27         11
#:     run62    1762    4/6        34         12
#:
#: every long run carrying `exec.bash.run FAILED - ====== test session starts
#: ======`. The command did not fail. The TESTS failed, which is a finding.
TEST_FAILED_NOTE = (
    "the test runner RAN and reported {summary}. A non-zero exit is how a "
    "runner says some tests failed - the command itself worked, and this is "
    "the ANSWER to what you asked. Re-running it unchanged returns the same "
    "thing. Read the failure above, fix the code or the test it names, and run "
    "it again only after you have changed something.")

#: The opposite, and worth its own sentence because it looks like success:
#: a runner that collected nothing exits 0 under unittest ("Ran 0 tests ... OK")
#: and 5 under pytest, and either way nothing was verified. Named in the
#: original census evidence for this goal.
NO_TESTS_NOTE = (
    "the test runner found NO TESTS to run ({summary}). Nothing was verified, "
    "whatever the exit code says. That is almost always the working directory "
    "or the path: run it from the directory that CONTAINS the package, and "
    "point the runner at the tests directory or a file that matches the "
    "runner's discovery pattern (`test_*.py`).")

#: A TEST FILE run directly as a script. `python test_stats.py` does not run
#: the tests - it runs the module's top level, usually a `main()` the model
#: wrote into it - and any exception comes back as a plain traceback with no
#: runner summary, so the runner notes above never fire. Census run67,
#: build-multifile: eleven `exec.python.run FAILED`, every one a Traceback out of
#: `test_stats.py ... main()`, retried with small variations because nothing said
#: what was actually wrong with the CALL.
SCRIPT_RUN_NOTE = (
    "you ran the test file `{name}` directly as a script, so its tests did not "
    "run - only the module's top level did, and that is what raised. A test "
    "file is run by a runner: `python -m pytest -q {name}` (or "
    "`python -m unittest {module}`) from the directory that contains the "
    "package. Re-running it as a script will raise the same thing.")

#: How much of the command to carry back. Enough to identify the call, not
#: enough to bloat every result.
MAX_COMMAND = 300

#: `python test_x.py`, `python3 ./tests/test_x.py`, `python -u path/test_x.py`.
#: NOT `python -m pytest test_x.py` (a runner) and NOT `python -m test_x`.
_SCRIPT_RUN = re.compile(
    r"(?:^|[\s;&|])(?:python[0-9.]*|py)\s+(?:-[a-zA-Z]\s+)*((?:[\w./-]*/)?test_[\w-]+\.py)\b")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")


def script_run_of_test_file(res: Any) -> str:
    """The test file a command ran as a script, or "" when this is not that.

    Needs all three: the command names a `test_*.py` given straight to the
    interpreter (no `-m`), the run failed, and the output is a traceback with
    no runner summary in it - a runner's own failure is the runner's to
    explain, and `test_run_summary` already does.
    """
    if not looks_like_exec_result(res):
        return ""
    rc = _rc_of(res)
    if rc in (None, 0):
        return ""
    cmd = _txt(res.get("command"))
    if " -m " in " " + cmd + " " or "pytest" in cmd or "unittest" in cmd:
        return ""
    m = _SCRIPT_RUN.search(cmd)
    if not m:
        return ""
    blob = _txt(res.get("stdout")) + "\n" + _txt(res.get("stderr"))
    if not _TRACEBACK.search(blob):
        return ""
    if test_run_summary(res):
        return ""
    return m.group(1)

#: pytest's own summary line: "3 failed, 5 passed in 0.42s". Counted by outcome
#: so the note can quote real numbers rather than say "some".
_PYTEST_COUNT = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b",
    re.I)
#: "===== no tests ran in 0.01s =====" / "collected 0 items"
_PYTEST_NONE = re.compile(r"\bno tests ran\b|\bcollected 0 items\b", re.I)
#: unittest: "Ran 5 tests in 0.003s", then "OK" or "FAILED (failures=2, errors=1)"
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_UNITTEST_BAD = re.compile(r"^FAILED \((.*?)\)\s*$", re.M)
#: Enough to say a runner produced this at all.
_RUNNER_MARKS = ("test session starts", "=== FAILURES ===", "short test summary",
                 "Ran 0 tests", "Ran 1 test", "collected ", "no tests ran",
                 "pytest", "unittest")


def test_run_summary(res: Any) -> Optional[Dict[str, Any]]:
    """What a test runner reported, or None when this is not a test run.

    Returns {"ran": bool, "counts": {outcome: n}, "text": "3 failed, 5 passed"}.
    Deliberately conservative: a command is only treated as a test run when its
    output carries a runner's own summary, so `echo pytest` is not one.
    """
    if not looks_like_exec_result(res):
        return None
    blob = (_txt(res.get("stdout")) + "\n" + _txt(res.get("stderr"))).strip()
    if not blob:
        return None

    counts: Dict[str, int] = {}
    for n, word in _PYTEST_COUNT.findall(blob):
        key = "errors" if word.lower().startswith("error") else word.lower()
        try:
            counts[key] = counts.get(key, 0) + int(n)
        except ValueError:
            continue

    m_ran = _UNITTEST_RAN.search(blob)
    ran_n = int(m_ran.group(1)) if m_ran else None
    m_bad = _UNITTEST_BAD.search(blob)

    none_ran = bool(_PYTEST_NONE.search(blob)) or ran_n == 0
    looks_like_a_run = (
        bool(counts) or m_ran is not None or none_ran
        or any(mark.lower() in blob.lower() for mark in _RUNNER_MARKS))
    if not looks_like_a_run:
        return None

    if none_ran:
        text = "no tests ran" if ran_n is None else "ran 0 tests"
        return {"ran": False, "counts": counts, "text": text}

    if counts:
        order = ("failed", "errors", "passed", "skipped", "xfailed", "xpassed")
        text = ", ".join("%d %s" % (counts[k], k) for k in order if counts.get(k))
    elif m_bad:
        text = "%d tests run, FAILED (%s)" % (ran_n or 0, m_bad.group(1))
    elif ran_n:
        text = "%d test%s run" % (ran_n, "" if ran_n == 1 else "s")
    else:
        return None
    return {"ran": True, "counts": counts, "text": text}


def is_test_failure(res: Any) -> bool:
    """A non-zero exit from a runner that DID run tests and report on them."""
    if _rc_of(res) in (None, 0):
        return False
    s = test_run_summary(res)
    return bool(s and s.get("ran"))


def found_no_tests(res: Any) -> bool:
    """A runner that collected nothing - whatever it exited with."""
    s = test_run_summary(res)
    return bool(s and not s.get("ran"))


def _txt(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def looks_like_exec_result(res: Any) -> bool:
    """True for an exec-shaped result dict, false for everything else."""
    if not isinstance(res, dict):
        return False
    return (any(k in res for k in _RC_KEYS)
            and any(k in res for k in _OUT_KEYS))


def _rc_of(res: Dict[str, Any]) -> Optional[int]:
    for k in _RC_KEYS:
        if k in res:
            try:
                return int(res[k])
            except (TypeError, ValueError):
                return None
    return None


def is_silent_success(res: Any) -> bool:
    """A command that finished cleanly and wrote nothing to stdout OR stderr.

    Deliberately strict. A timed-out command is not a success even if it printed
    nothing, and a non-zero rc obviously is not — in both of those the blank
    output is genuinely a missing signal rather than an answer.
    """
    if not looks_like_exec_result(res):
        return False
    if res.get("timed_out"):
        return False
    rc = _rc_of(res)
    if rc is None or rc != 0:
        return False
    if res.get("ok") is False:
        return False
    return not (_txt(res.get("stdout")).strip() or _txt(res.get("stderr")).strip())


def is_silent_failure(res: Any) -> bool:
    """A command that exited NON-ZERO and wrote nothing to stdout OR stderr.

    Strict for the same reasons as its twin: a timeout is not a silent failure
    (it has its own cause, and `result_failure_reason` reports it), and a
    negative rc means the process never ran - executable missing, killed by a
    signal - which is a genuine fault rather than a negative answer.
    """
    if not looks_like_exec_result(res):
        return False
    if res.get("timed_out"):
        return False
    rc = _rc_of(res)
    if rc is None or rc <= 0:
        return False
    return not (_txt(res.get("stdout")).strip() or _txt(res.get("stderr")).strip())


def annotate(res: Any, command: Any = "") -> Any:
    """Return `res` with the command and any applicable note attached.

    Three things can be added: the COMMAND (so a failure can name the call it
    came from), the silent-SUCCESS note, and the silent-FAILURE note. Never
    mutates the caller's dict, and never overwrites an existing `note` or
    `command` - a caller that already explained itself knows more about the
    specific command than this does.
    """
    # Anything that is not an exec result dict passes straight through. The
    # previous version got this for free because is_silent_success() was the
    # first thing it called; doing work before that check reintroduced it, and
    # test_a_non_exec_result_is_never_touched caught it with a list.
    if not looks_like_exec_result(res):
        return res

    add: Dict[str, Any] = {}

    # The command, so a failure can name the call it came from. Without this
    # `result_failure_reason` has nothing to identify an exec failure by, and
    # its "name the command" branch - written precisely so the reader is "not
    # reduced to guessing which call this was" - is dead code for every exec
    # capability. Attached on success too: it costs one field and makes a
    # result readable on its own.
    cmd = _txt(command).strip()
    if cmd and not _txt(res.get("command")).strip():
        add["command"] = cmd[:MAX_COMMAND]

    # Never overwrite an existing note - a caller that already explained itself
    # knows more about the specific command than this does.
    if not _txt(res.get("note")).strip():
        _tests = test_run_summary(res)
        # The command is looked up on the result AS ANNOTATED, so a caller that
        # passed it in `command=` is seen here too.
        _script = script_run_of_test_file(dict(res, command=cmd or _txt(res.get("command"))))
        if _tests and not _tests["ran"]:
            add["note"] = NO_TESTS_NOTE.format(summary=_tests["text"])
        elif _tests and _rc_of(res) not in (None, 0):
            add["note"] = TEST_FAILED_NOTE.format(summary=_tests["text"])
        elif _script:
            _mod = _script.rsplit("/", 1)[-1][:-3]
            add["note"] = SCRIPT_RUN_NOTE.format(name=_script, module=_mod)
        elif is_silent_success(res):
            add["note"] = NOTE
        elif is_silent_failure(res):
            add["note"] = FAILURE_NOTE.format(rc=_rc_of(res))

    if not add:
        return res
    out = dict(res)
    out.update(add)
    return out
