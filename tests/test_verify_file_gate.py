"""Verifier file-existence hard-gate must not fire on a LISTING criterion (2026-08-17):
a planning step "generate a list of required files (index.html, style.css, script.js)"
was auto-failed because the verifier extracted those filenames and demanded they exist.
The gate should apply only when the criterion requires files to be PRODUCED, not merely
listed/identified. Imports the monolith, runs in-container.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag.dag_workshop_capabilities import (  # noqa: E402
    _V6_FILE_CRIT_RE as FC, _V6_FILE_LIST_CRIT_RE as FL)


def _hardgates(crit):
    # mirrors _v6_verify_step's decision
    return (bool(FC.search(crit)) or "/workspace" in crit) and not FL.search(crit)


def test_listing_criterion_is_not_hardgated():
    # the exact step-1 criterion from the failing run
    c = ("A clear list of required files (index.html, style.css, script.js) "
         "and core logic points is generated.")
    assert _hardgates(c) is False


def test_identify_outline_describe_not_hardgated():
    assert _hardgates("Identify the files needed and describe the core logic points") is False
    assert _hardgates("Enumerate the required source files and their responsibilities") is False


def test_real_file_creation_criteria_still_hardgated():
    # the step-2 criterion from the same run — genuinely requires the file on disk
    assert _hardgates("The filesystem contains an 'index.html' file (or equivalent) ready "
                      "for content.") is True
    assert _hardgates("index.html is created with the pomodoro UI") is True
    assert _hardgates("the script is written to /workspace/app.py") is True
