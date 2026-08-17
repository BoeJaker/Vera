"""Phase: loop code-authoring fast-path (vera/dag/dag_workshop_capabilities.py).

Locks the pure decision behind the deterministic verifier fast-path: a step whose
terminal action is a SUCCESSFUL code.author/code.edit is 'met' WITHOUT a second LLM
judge on the code (its syntax was already parser-verified). A run/fetch/failed
author is NOT short-circuited — those keep the normal output-grounded judge, so the
run's OUTPUT is what gets evaluated, never the code itself.

Imports the monolith module, so it runs in-container (evolve.unittest.run), not the
pure critical gate — kept deliberately out of the critical set for that reason.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag.dag_workshop_capabilities import (  # noqa: E402
    _v6_authored_path, _v6_finalize_step)


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


# ── finalize fast-path: no LLM distil for a just-authored code step ──────────
# The `model="__dummy__"` would make any real LLM call fail; the short-circuit
# must return a deterministic summary WITHOUT ever reaching that call.
def test_finalize_short_circuits_authored_code_no_llm():
    long_raw = "verbose specialist chatter that would normally be distilled. " * 12  # >400 chars
    assert len(long_raw) >= 400
    step = {"success": "build.py exists", "goal": "write build.py"}
    res = {"ok": True, "summary": long_raw, "outputs": {},
           "history": [{"tool": "code.author", "ok": True, "args": {"path": "build.py"}}]}
    asyncio.run(_v6_finalize_step(step, res, goal="g", model="__dummy__",
                                  instance_id="", prefer_gpu=False, session_id=""))
    assert res.get("finalized") is True
    assert res.get("raw_summary") == long_raw          # original preserved
    assert "build.py" in res["summary"] and "exec.python.run" in res["summary"]
    assert res["summary"] != long_raw                   # replaced with the concise line


def test_finalize_does_not_short_circuit_non_author_terminal():
    # terminal action is a RUN, not an author -> our short-circuit must NOT fire
    # (it would then fall through to the LLM path, which with a dummy model returns
    # early and leaves finalized unset — so 'finalized' must not be True from us).
    long_raw = "verbose specialist chatter that would normally be distilled. " * 12
    step = {"success": "prints output", "goal": "run build.py"}
    res = {"ok": True, "summary": long_raw, "outputs": {},
           "history": [{"tool": "exec.python.run", "ok": True, "args": {"path": "build.py"}}]}
    asyncio.run(_v6_finalize_step(step, res, goal="g", model="__dummy__",
                                  instance_id="", prefer_gpu=False, session_id=""))
    assert res.get("finalized") is not True             # our fast-path did not fire
    assert res["summary"] == long_raw                   # untouched by us
