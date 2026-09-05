"""Census 34's two most common failures, neither of which was a missing guard.

    exec.bash.run FAILED - failed with no error detail (rc=92, keys=[
      'elapsed_ms','ok','rc','sandboxed','stderr','stdout','timed_out'])

    code.edit FAILED               - missing required argument: path
    sandbox.session.fs.read FAILED - path required            (x3)

Both already had handling. The first has an extractor that checks eight text
fields - and the cause was a BOOLEAN sitting in the same dict, so every text
field was legitimately empty and the model was told nothing. The second already
refuses the call and re-prompts, but told the model only that an argument was
missing, so it guessed again; the run's artifact registry knew exactly which
files existed.

Neither fix invents an answer. The timeout reports what happened to the call;
the path hint NAMES the candidates rather than choosing one, because picking a
file on the model's behalf can edit the wrong one - the same reasoning that
keeps _v5_apply_edits refusing an ambiguous anchor.

Pure: no I/O.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag.result_failure_reason import failure_reason      # noqa: E402
from vera.dag.missing_arg_hint import describe                 # noqa: E402

pytestmark = pytest.mark.critical

#: The exact shape census 34 reported.
CENSUS34 = {"elapsed_ms": 25000, "ok": False, "rc": 92, "sandboxed": True,
            "stderr": "", "stdout": "", "timed_out": True}


# ── a timeout is a cause ───────────────────────────────────────────────────

def test_the_census34_result_now_explains_itself():
    r = failure_reason(CENSUS34, 92)
    assert "no error detail" not in r
    assert "did not finish in time" in r


def test_a_real_error_still_beats_the_flag():
    """The flag is a last resort; anything the call actually SAID wins."""
    assert failure_reason({"stderr": "boom", "timed_out": True}, 1) == "boom"


def test_each_flag_names_what_happened_to_the_call():
    for field, fragment in (("killed", "killed"), ("cancelled", "cancelled"),
                            ("truncated", "truncated")):
        assert fragment in failure_reason({field: True, "rc": 1}, 1)


def test_a_false_flag_is_not_a_cause():
    r = failure_reason({"timed_out": False, "killed": False, "rc": 3}, 3)
    assert "no error detail" in r


def test_a_genuinely_silent_failure_names_the_command():
    """So the next reader is not reduced to guessing which call this was."""
    r = failure_reason({"rc": 92, "stdout": "", "stderr": "",
                        "command": "python3 build.py"}, 92)
    assert "python3 build.py" in r


def test_a_silent_failure_with_nothing_at_all_still_reports_cleanly():
    assert "rc=5" in failure_reason({"ok": False, "rc": 5}, 5)


# ── a missing path, with the candidates named ──────────────────────────────

ARTS = {"timer.html": {"rel": "timer.html"},
        "notes.md": {"rel": "notes.md"},
        "url:http://x": {"_url_cache_value": 1}}


def test_the_files_the_run_wrote_are_named():
    r = describe("code.edit", ["path"], ARTS)
    assert "timer.html" in r and "notes.md" in r


def test_url_cache_entries_are_not_offered_as_files():
    """A candidate the model cannot open is worse than none."""
    assert "url:http://x" not in describe("code.edit", ["path"], ARTS)


def test_a_single_file_is_pointed_at_directly():
    r = describe("code.edit", ["path"], {"timer.html": {"rel": "timer.html"}})
    assert "exactly one file, timer.html" in r


def test_an_empty_registry_says_there_is_nothing_to_name():
    r = describe("code.edit", ["path"], {})
    assert "has not written any files" in r


def test_it_never_fills_the_argument_in():
    """Choosing a file on the model's behalf can edit the wrong one."""
    r = describe("code.edit", ["path"], ARTS)
    assert "do not guess" in r
    assert r.startswith("missing required argument: path")


def test_a_non_path_argument_gets_the_plain_message():
    """Inventing help for an argument we know nothing about is noise."""
    assert describe("http.get", ["url"], ARTS) == "missing required argument: url"


def test_several_missing_arguments_are_all_named():
    r = describe("code.edit", ["path", "task"], ARTS)
    assert "path, task" in r


def test_no_missing_arguments_is_not_a_sentence_about_files():
    assert describe("code.edit", [], ARTS) == "missing required argument: "


def test_the_hint_survives_a_registry_of_odd_shapes():
    for junk in (None, {}, {"a": None}, {"b": "plain-string"}, {"c": {"rel": ""}}):
        assert describe("code.edit", ["path"], junk).startswith("missing required")


def test_the_executor_actually_uses_the_hint():
    """The helper is inert unless the refusal path calls it. Pinned on the
    module source: the executor is one enormous function and importing it to
    drive a refusal would pull in the whole orchestrator."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                        "dag_workshop_capabilities.py")
    src = open(path, encoding="utf-8").read()
    assert "_missing_arg_hint.describe(tool, _missing_req, artifacts)" in src
    # ...and the old bare message must be gone from that refusal, or the hint
    # is built and thrown away.
    assert src.count('"missing required argument: " + ", ".join(_missing_req)') == 0
