"""A command that succeeded and printed nothing has told you something.

Census run 16, build-multifile, step 8. Twelve calls, ten of which SUCCEEDED,
five of them returning rc=0 with an empty stdout:

    exec.python.run .../tests/test_stats.py    rc=0  stdout=""
    python  -m py_compile .../test_stats.py    rc=0  stdout=""
    (again)                                    rc=0  stdout=""
    python3 -m py_compile ...                  rc=0  stdout=""
    python  -m py_compile ...                  rc=0  stdout=""

py_compile on a valid file prints nothing, and running a pytest-style file
directly prints nothing. So the model was handed an empty string five times and
could not tell whether it had verified anything - an empty stdout reads as "no
information", not "the check passed" - so it varied the command slightly and
tried again: python then python3, absolute then relative, `ls &&` prefixed.

The loop's guards were NOT bypassed and were NOT missing. success_sigs
de-duplicates identical repeats; failed_sigs/_MAX_REPEAT_FAIL catches identical
failures. These calls were neither identical nor failing. Nothing was broken
except the model's ability to draw a conclusion, so the answer is not another
guard - it is to stop shipping an ambiguous blank.

Pure: no Redis, no app import.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.execution.exec_result_note import (          # noqa: E402
    NOTE, annotate, is_silent_success, looks_like_exec_result,
)


def _ok(**kw):
    r = {"ok": True, "rc": 0, "stdout": "", "stderr": ""}
    r.update(kw)
    return r


# ── the case this exists for ────────────────────────────────────────────────
def test_the_real_py_compile_result_is_annotated():
    """The exact shape step 8 received five times."""
    res = annotate({"ok": True, "rc": 0, "stdout": "", "stderr": "",
                    "timed_out": False, "sandboxed": True, "elapsed_ms": 40})
    assert res["note"] == NOTE
    assert "empty stdout here is the result" in res["note"]


def test_the_note_tells_it_what_to_do_instead():
    """'It succeeded' alone would still leave it re-running to be sure. The note
    has to close off the retry AND say how to get output if it wants some."""
    assert "Do not re-run it" in NOTE
    assert "vary it slightly and retry" in NOTE
    assert "print" in NOTE


def test_whitespace_only_output_still_counts_as_silent():
    assert is_silent_success(_ok(stdout="\n  \t\n")) is True


# ── what must NOT be annotated ─────────────────────────────────────────────
def test_a_failure_is_never_a_silent_success():
    assert is_silent_success(_ok(ok=False, rc=1)) is False
    assert is_silent_success(_ok(rc=127)) is False


def test_a_timeout_is_not_a_success_even_when_it_printed_nothing():
    """Here the blank output really IS a missing signal, not an answer."""
    assert is_silent_success(_ok(timed_out=True)) is False
    assert is_silent_success(_ok(rc=124, ok=False, timed_out=True)) is False


def test_a_command_with_output_is_left_alone():
    assert is_silent_success(_ok(stdout="total 16")) is False
    assert is_silent_success(_ok(stderr="warning: deprecated")) is False
    assert "note" not in annotate(_ok(stdout="hi"))


def test_a_non_exec_result_is_never_touched():
    """Plenty of capabilities return an `ok`; annotating them would be noise at
    best and misleading at worst."""
    for other in ({"ok": True}, {"ok": True, "items": []},
                  {"ok": True, "rc": 0}, {"ok": True, "stdout": ""},
                  {"count": 0}, [], "", None, 42):
        assert looks_like_exec_result(other) is False, other
        assert annotate(other) is other


def test_an_existing_note_is_never_overwritten():
    """A caller that already explained itself knows more about the specific
    command than this does."""
    r = _ok(note="killed after 30s - this command did not exit on its own")
    assert annotate(r) is r
    assert "did not exit" in annotate(r)["note"]


# ── shape and safety ───────────────────────────────────────────────────────
def test_the_callers_dict_is_never_mutated():
    original = _ok()
    out = annotate(original)
    assert "note" not in original and "note" in out


def test_alternative_return_code_keys_are_understood():
    """Different runners spell it rc / exit_code / returncode."""
    assert is_silent_success({"exit_code": 0, "stdout": "", "stderr": ""}) is True
    assert is_silent_success({"returncode": 0, "stdout": "", "stderr": ""}) is True
    assert is_silent_success({"exit_code": 2, "stdout": "", "stderr": ""}) is False


def test_an_unparseable_return_code_is_not_assumed_successful():
    assert is_silent_success({"rc": "fine", "stdout": "", "stderr": ""}) is False
    assert is_silent_success({"rc": None, "stdout": "", "stderr": ""}) is False


def test_non_string_output_does_not_crash_the_check():
    assert is_silent_success({"rc": 0, "stdout": None, "stderr": None}) is True
    assert is_silent_success({"rc": 0, "stdout": 0, "stderr": ""}) is False


# ── every exec path must go through it ─────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_exec_result_shaper_annotates():
    """Comprehensive by construction: the local shell, ssh, and both sandbox
    backends. Patching some and not others is how the PYTHONPATH prefix drifted
    into three copies, two of which were broken."""
    ec = _src("vera", "execution", "exec_capabilities.py")
    assert ec.count("_exec_result_note.annotate(") >= 2, "local shell + ssh"
    sb = _src("vera", "remote", "session_sandbox_capabilities.py")
    assert sb.count("_exec_result_note.annotate(") >= 2, "sandbox local + docker"


def test_the_loop_annotates_at_its_result_chokepoint():
    """The backstop: every capability result the model reads passes through
    _result_preview, so an exec path nobody remembered still gets the note."""
    dag = _src("vera", "dag", "dag_workshop_capabilities.py")
    at = dag.find("def _result_preview(")
    assert at > 0
    body = dag[at:at + 1400]
    assert "annotate(result)" in body, (
        "_result_preview must annotate, so this holds for exec caps reached by "
        "any route rather than only the ones patched by hand")
