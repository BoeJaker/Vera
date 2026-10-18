"""Lint on a change's changed lines (looplab-control §9 K6, 18 Oct 2026): the pure core and the capability's shape."""
import os

import pytest

from vera.evolve import diff_lint_core as core

pytestmark = pytest.mark.critical

DIFF = "\n".join([
    "diff --git a/vera/a.py b/vera/a.py",
    "--- a/vera/a.py",
    "+++ b/vera/a.py",
    "@@ -1,3 +1,4 @@",
    " x = 1",
    "-y = 2",
    "+y = 3   ",
    "+<<<<<<< HEAD",
    " z = 4",
    "diff --git a/old.txt b/old.txt",
    "deleted file mode 100644",
    "--- a/old.txt",
    "+++ /dev/null",
    "@@ -1 +0,0 @@",
    "-gone",
])


def test_the_diff_parses_with_new_line_numbers_of_added_lines():
    files = core.parse_unified(DIFF)
    assert [f["path"] for f in files] == ["vera/a.py", "old.txt"]
    assert files[0]["added"] == {2: "y = 3   ", 3: "<<<<<<< HEAD"}
    assert files[1]["deleted"] is True and files[1]["added"] == {}


def test_line_checks_find_what_a_changed_line_carries():
    f = core.parse_unified(DIFF)[0]
    got = {(x["line"], x["code"]) for x in core.line_checks(f["path"], f["added"])}
    assert (2, "W291") in got and (3, "CONFLICT") in got
    assert core.line_checks("a.js", {5: "\tdebugger;"}) and {x["code"] for x in core.line_checks("a.js", {5: "\tdebugger;"})} == {"W191", "DEBUGGER"}
    assert core.line_checks("x.py", {1: "a\x00b"})[0]["severity"] == "error"


def test_python_syntax_names_the_line_and_the_check():
    assert core.py_syntax("a.py", "x = 1\n") == []
    f = core.py_syntax("a.py", "x = 1\ndef (:\n")[0]
    assert f["line"] == 2 and f["code"] == "E999" and f["source"] == "python-ast" and f["severity"] == "error"


def test_findings_are_kept_on_changed_lines_and_every_error_is_kept():
    fs = [{"line": 2, "severity": "warning"}, {"line": 9, "severity": "warning"}, {"line": 40, "severity": "error"},
          {"line": None, "severity": "warning"}]
    assert [f["line"] for f in core.on_changed(fs, {2: "", 3: ""})] == [2, 40, None]


def test_node_output_becomes_a_finding_and_inline_scripts_join_like_the_panel_tests():
    f = core.node_finding("a.js", "[stdin]:7\nfoo(\n^\nSyntaxError: Unexpected end of input", joined=False)[0]
    assert f["line"] == 7 and "Unexpected end of input" in f["message"] and f["source"] == "node"
    assert core.node_finding("p.html", "[stdin]:7\nSyntaxError: x", joined=True)[0]["line"] is None
    assert core.node_finding("a.js", "", joined=False) == []
    assert core.inline_scripts('<script src="x.js"></script><script>a()</script><script type="module">b()</script>') == "a()\n;\nb()"


def test_a_check_that_cannot_run_is_never_clean():
    s = core.summary([], {"line-checks": "ok", "ruff": "absent"})
    assert s["clean"] is False and s["absent"] == ["ruff"]
    assert core.summary([], {"line-checks": "ok"})["clean"] is True


def test_the_capability_reads_the_pipelines_own_diff_and_reports_absent_checkers():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "evolve", "diff_lint_capabilities.py"), encoding="utf-8").read()
    assert '@capability("evolve.pipeline.lint"' in src and '_call("evolve.pipeline.diff", id=id' in src
    assert '"ruff": "ok" if ruff else "absent"' in src and '"node": "ok" if node else "absent"' in src
    orch = open(os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "evolve/diff_lint_capabilities.py")' in orch
