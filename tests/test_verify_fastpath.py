"""Phase: loop code-authoring fast-path (vera/dag/dag_workshop_capabilities.py).

Locks the pure decision behind the deterministic verifier fast-path: a step whose
terminal action is a SUCCESSFUL code.author/code.edit is 'met' WITHOUT a second LLM
judge on the code (its syntax was already parser-verified). A run/fetch/failed
author is NOT short-circuited — those keep the normal output-grounded judge, so the
run's OUTPUT is what gets evaluated, never the code itself.

Imports the monolith module, so it runs in-container (evolve.unittest.run), not the
pure critical gate — kept deliberately out of the critical set for that reason.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag.dag_workshop_capabilities import _v6_authored_path  # noqa: E402


def _call(tool, ok=True, path="x.py"):
    return {"tool": tool, "ok": ok, "args": {"path": path}}


def test_successful_code_author_short_circuits():
    assert _v6_authored_path(_call("code.author", ok=True, path="build.py")) == "build.py"
    assert _v6_authored_path(_call("code.edit", ok=True, path="fix.py")) == "fix.py"


def test_failed_author_does_not_short_circuit():
    # ok=False means the parser rejected it — must NOT be treated as met
    assert _v6_authored_path(_call("code.author", ok=False, path="build.py")) == ""


def test_non_author_tools_never_short_circuit():
    # a run/fetch/transform keeps the normal output judge (eval the OUTPUT)
    assert _v6_authored_path(_call("exec.python.run", ok=True, path="build.py")) == ""
    assert _v6_authored_path(_call("http.get", ok=True, path="")) == ""
    assert _v6_authored_path(_call("exec.bash.run", ok=True, path="build.py")) == ""


def test_author_without_path_returns_empty():
    assert _v6_authored_path({"tool": "code.author", "ok": True, "args": {}}) == ""
    assert _v6_authored_path({"tool": "code.author", "ok": True}) == ""


def test_malformed_inputs_are_safe():
    assert _v6_authored_path(None) == ""
    assert _v6_authored_path({}) == ""
    assert _v6_authored_path({"tool": "code.author"}) == ""   # no ok flag -> falsy
