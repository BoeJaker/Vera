"""Error recovery keeps the original call's identity (plan item 21).

run70-73 (24 Sep 2026): recovery answers moved a sandbox browser run to
prod's own UI (`kind: live`), granted `allow_destructive: true` and
`allowlist: ["*"]`, and invented providers. What a call asks for may be
reshaped; where it runs and what it may do may not.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.recovery_identity_core import keep_identity  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
ORIG = {"goal": "type not-an-email and read the error", "kind": "sandbox",
        "url": "https://localhost:8999/remote/sandbox/preview/558d6a15/form.html", "max_steps": 8}


def test_the_census_recovery_is_pulled_back_to_the_original_target():
    rec = {"goal": "Focus the email field, type character by character", "kind": "live",
           "url": "file:///workspace/form.html", "base_url": "", "provider": "local", "model": "fast-8b",
           "allowlist": ["*"], "allow_destructive": True, "max_steps": 10, "max_seconds": 60}
    out, notes = keep_identity(ORIG, rec)
    assert out["kind"] == "sandbox"
    assert out["url"] == ORIG["url"]
    assert "allow_destructive" not in out and "allowlist" not in out and "provider" not in out
    assert out["goal"] == rec["goal"] and out["max_steps"] == 10          # what it asks for may change
    assert any("kind: recovery changed it" in n for n in notes)
    assert any("allow_destructive: recovery introduced" in n for n in notes)


def test_a_recovery_that_keeps_the_identity_is_untouched():
    rec = dict(ORIG, goal="try Tab instead of clicking away")
    out, notes = keep_identity(ORIG, rec)
    assert out == rec and notes == []


def test_the_url_may_move_within_the_host_but_not_to_another():
    rec = dict(ORIG, url="https://localhost:8999/remote/sandbox/preview/558d6a15/other.html")
    out, notes = keep_identity(ORIG, rec)
    assert out["url"].endswith("other.html") and notes == []
    rec2 = dict(ORIG, url="http://localhost:8994/ui/panel/window?id=None")
    out2, notes2 = keep_identity(ORIG, rec2)
    assert out2["url"] == ORIG["url"] and any("kept the original host" in n for n in notes2)


def test_a_provider_the_original_set_survives_and_one_it_did_not_is_dropped():
    o = dict(ORIG, provider="ollama")
    out, _ = keep_identity(o, dict(o, provider="playwright"))
    assert out["provider"] == "ollama"
    out2, notes2 = keep_identity(ORIG, dict(ORIG, provider="local-sandbox"))
    assert "provider" not in out2 and notes2


def test_a_run_that_stopped_on_its_budget_is_not_an_arg_error():
    """run77 long-horizon: the browser's time-budget stop was recovered twice, 1,554 s for one call."""
    from vera.dag.recovery_identity_core import is_run_stop, is_long_run_tool
    stop = ("time_budget: stopped after 511s (budget 480s) having taken 8 step(s) without reaching the "
            "goal. This is a TIME limit, not evidence about the page. Expected: the countdown; got: ...")
    assert is_run_stop(stop)
    assert is_run_stop("no_progress: stopped after 5 consecutive actions that changed nothing")
    assert is_run_stop("repeating_action: the same action was repeated 5 times")
    assert not is_run_stop("path is required and none was given")
    assert not is_run_stop("missing required argument: url")
    assert is_long_run_tool("operator.run") and not is_long_run_tool("http.get")


def test_the_classifier_and_the_executor_refuse_to_recover_a_run_stop():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_is_arg_error")
    body = ast.get_source_segment(src, fn)
    assert "_recovery_identity.is_run_stop(e)" in body
    assert body.index("is_run_stop(e)") < body.index("NON_RECOVERABLE")
    assert "not _recovery_identity.is_long_run_tool(tool)" in src


def test_the_recovery_path_applies_it_before_the_model_heal():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_attempt_arg_recovery")
    body = ast.get_source_segment(src, fn)
    assert "_recovery_identity.keep_identity(failed_args, coerced)" in body
    assert body.index("keep_identity(") < body.index("_heal_model_arg(cap_name, coerced")
