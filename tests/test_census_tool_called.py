"""Census check `tool_called` (owner 12 Oct 2026): an operator-family goal passes only if operator.run was
actually called - "verify in a browser" via http.server + curl is not the operator."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.census import quality as Q  # noqa: E402

RC = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical
CHECK = {"tool_called": "operator.run", "why": "the BROWSER check used the operator"}


def test_tool_called_passes_only_when_called():
    assert Q.evaluate_one(CHECK, {}, "", ["code.author", "operator.run"])["ok"]
    r = Q.evaluate_one(CHECK, {}, "", ["code.author", "exec.bash.run"])
    assert not r["ok"] and "never called" in r["detail"]
    u = Q.evaluate_one(CHECK, {}, "", None)
    assert not u["ok"] and "not available" in u["detail"]


def test_evaluate_threads_tools_and_old_calls_still_work():
    out = Q.evaluate([CHECK, {"answer_contains": "144"}], {}, "it says 144", tools=["operator.run"])
    assert out["score"] == 1.0
    assert Q.evaluate([{"answer_contains": "x"}], {}, "x")["score"] == 1.0


def test_harness_passes_the_trace_tools():
    assert "quality = _q_eval(g.get(\"checks\"), got, answer, tools=_tools)" in RC
