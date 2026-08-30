"""Three ways a loop step burns cycles without learning anything.

All three come from census build-multifile (2026-08-29), which spent 21 cycles
before its first real test run:

  * code.author with no path silently wrote "generated.py" and reported ok=true,
    three times, so nothing told the model its call was malformed;
  * exec.python.run ran /workspace/statkit/test_stats.py directly. Python puts the
    SCRIPT'S directory on sys.path[0], never the cwd, so `from statkit.stats import
    ...` raised ModuleNotFoundError four times. Adding __init__.py did not help and
    could not: the workspace root was never on the path. The same goal succeeded the
    moment pytest ran with rootdir=/workspace;
  * code.edit refused an ambiguous `find` with "make it unique by including more
    surrounding context" - true, but it never said WHERE the matches were.
"""
import ast
import os

import pytest

HERE = os.path.dirname(__file__)
SBX = os.path.join(HERE, "..", "vera", "remote", "session_sandbox_capabilities.py")
DAG = os.path.join(HERE, "..", "vera", "dag", "dag_workshop_capabilities.py")


def _functions(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, (ast.get_source_segment(src, node) or "")


def test_every_sandbox_interpreter_runner_puts_the_workspace_on_pythonpath():
    """Swept by SHAPE, not by name: a third runner added later is caught too.

    The prefix now has ONE definition (`_pythonpath_prefix`) rather than three
    inline copies, because the copies drifted: the two wrapped by `_bounded_cmd`
    were silently broken by the `timeout` wrapper while the unwrapped one kept
    working (2026-08-30, census run 16). So a runner satisfies this by CALLING
    the helper — accepting a hand-rolled assignment as well would re-open the
    door to exactly the divergence that hid the bug.
    """
    runners = {n: b for n, b in _functions(SBX) if "' '.join(prefix)" in b}
    assert len(runners) >= 2, "expected the inline and by-path runners, got %s" % list(runners)
    for name, body in runners.items():
        assert "_pythonpath_prefix()" in body, (
            "%s runs an interpreter without the workspace root on PYTHONPATH - a "
            "package-relative import from a loop-authored project cannot resolve. "
            "Use _pythonpath_prefix(); a hand-rolled 'PYTHONPATH=' assignment "
            "breaks under the timeout wrapper." % name)


def test_the_authoring_caps_no_longer_invent_a_filename():
    src = open(DAG, encoding="utf-8").read()
    # The FALLBACK expression, not any mention - the comment explaining the fix
    # legitimately names the old filename.
    for bad in ('or "generated.py"', 'or "generated.md"'):
        assert bad not in src, "an authoring cap still invents a filename: %s" % bad
    assert '"path is required' in src, "the missing-path error went away"


try:
    from Vera.vera.dag import dag_workshop_capabilities as W
except Exception:                                      # pragma: no cover
    W = None


@pytest.mark.skipif(W is None, reason="app module not importable here")
def test_this_module_actually_imported_the_app():
    assert W is not None and hasattr(W, "_v5_apply_edits")


@pytest.mark.skipif(W is None, reason="app module not importable here")
def test_an_ambiguous_edit_names_the_lines_it_matched():
    content = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    res = W._v5_apply_edits(content, [{"find": "    return 1", "replace": "    return 2"}])
    assert res["ok"] is False
    err = " ".join(res["errors"])
    assert "matches 2 places" in err, err
    # the whole point: it must say WHERE, so the editor can extend the match
    # "(lines 2, 5)" exactly - asserting "2" alone would pass on "matches 2 places"
    assert "(lines 2, 5)" in err, "the matching line numbers are not named: %s" % err
    assert res["content"] == content, "an ambiguous edit must not be applied"


@pytest.mark.skipif(W is None, reason="app module not importable here")
def test_an_unambiguous_edit_still_applies():
    content = "def a():\n    return 1\n\ndef b():\n    return 7\n"
    res = W._v5_apply_edits(content, [{"find": "    return 7", "replace": "    return 8"}])
    assert res["ok"] is True, res
    assert "return 8" in res["content"] and "return 7" not in res["content"]


@pytest.mark.skipif(W is None, reason="app module not importable here")
def test_a_find_that_is_absent_still_reports_that_plainly():
    content = "def a():\n    return 1\n"
    res = W._v5_apply_edits(content, [{"find": "nope", "replace": "x"}])
    assert res["ok"] is False
    assert "not present" in " ".join(res["errors"])
