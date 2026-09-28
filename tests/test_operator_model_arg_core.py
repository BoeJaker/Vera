"""A made-up operator model must not cost a whole goal.

Census run57 (2026-09-21), `author-then-edit`: the executor called
`operator.run` with `model: "fast"`. Ollama 404'd every think, the operator
clicked blind until its guards fired, 1257 s gone. Two layers pin the fix:
the loop drops a model the cluster does not serve before the call, and the
thinker retries once without the model when Ollama says it is not found.

Pure: no Redis, no browser, no LLM (the thinker's LLM hop is injected).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag.operator_model_arg_core import (  # noqa: E402
    heal_model_arg, is_model_not_found_error, model_is_served, names_a_model,
    split_provider,
)

SERVED = ["jaahas/qwen3.5-uncensored:latest", "jaahas/qwen3.5-uncensored:9b",
          "qwen2.5:7b", "nomic-embed-text:latest"]


def test_the_census_case_drops_the_invented_model():
    args = {"url": "https://x/timer.html", "goal": "verify", "provider": "local", "model": "fast"}
    edits = heal_model_arg("operator.run", args, SERVED)
    assert edits and edits[0][0] == "model" and edits[0][1] == ""
    assert "'fast'" in edits[0][2] and "dropped" in edits[0][2]


def test_a_model_smuggled_in_through_provider_is_dropped_too():
    """Census run59 (2026-09-22): `provider: "ollama:fast-preview"` - the
    thinker splits provider on ":" and forwards the model; the heal must read
    the same field or the 404s reach the node three times in two seconds."""
    assert split_provider("ollama:fast-preview") == ("ollama", "fast-preview")
    assert split_provider("local") == ("local", "")
    assert split_provider(None) == ("", "")
    args = {"url": "https://x/timer.html", "goal": "verify", "provider": "ollama:fast-preview"}
    edits = heal_model_arg("operator.run", args, SERVED)
    assert edits == [("provider", "ollama", edits[0][2])]
    assert "'fast-preview'" in edits[0][2] and "no Ollama node serves" in edits[0][2]
    # a served model named through provider is left exactly as written
    assert heal_model_arg("operator.run", {"provider": "ollama:qwen2.5:7b"}, SERVED) == []
    assert heal_model_arg("operator.run", {"provider": "anthropic:claude-x"}, SERVED)[0][1] == "anthropic"
    # both fields wrong -> both healed
    both = heal_model_arg("operator.run", {"provider": "local:fast", "model": "faster"}, SERVED)
    assert [(f, v) for f, v, _ in both] == [("model", ""), ("provider", "local")]


def test_the_gate_sees_a_model_carried_by_provider_alone():
    """Operator census run3 (2026-09-24), operator-form-validation: the loop
    ran the heal only when args had a `model` key, so `provider:
    "ollama:fast-8b"` with no `model` reached Ollama and 404'd every think."""
    args = {"url": "https://x/form.html", "goal": "verify", "provider": "ollama:fast-8b"}
    assert names_a_model(args)
    edits = heal_model_arg("operator.run", args, SERVED)
    assert edits and edits[0][0] == "provider" and edits[0][1] == "ollama"
    assert "'fast-8b'" in edits[0][2]


def test_the_gate_is_quiet_when_no_model_is_named():
    assert not names_a_model({"url": "https://x", "goal": "g"})
    assert not names_a_model({"url": "https://x", "goal": "g", "provider": "ollama"})
    assert not names_a_model({"model": "   ", "provider": "local"})
    assert not names_a_model(None)
    assert names_a_model({"model": "fast"})
    assert names_a_model({"provider": "anthropic:claude-x"})   # named; the heal decides


def test_a_served_model_survives_in_any_spelling():
    for m in ("qwen2.5:7b", "jaahas/qwen3.5-uncensored", "jaahas/qwen3.5-uncensored:latest",
              "nomic-embed-text"):
        assert model_is_served(m, SERVED), m
        assert heal_model_arg("operator.run", {"model": m}, SERVED) == []
    assert not model_is_served("qwen2.5", SERVED)          # a different tag is a different model
    assert not model_is_served("", SERVED)


def test_no_catalogue_or_no_model_or_other_tool_means_no_edit():
    assert heal_model_arg("operator.run", {"model": "fast"}, []) == []
    assert heal_model_arg("operator.run", {"model": "fast"}, ["", None]) == []
    assert heal_model_arg("operator.run", {"model": ""}, SERVED) == []
    assert heal_model_arg("operator.run", {}, SERVED) == []
    assert heal_model_arg("operator.run", None, SERVED) == []
    assert heal_model_arg("code.author", {"model": "fast"}, SERVED) == []
    assert heal_model_arg("browser.navigate", {"model": "fast"}, SERVED)[0][0] == "model"


def test_not_found_error_shapes():
    assert is_model_not_found_error("Exception: ollama returned 404: {\"error\":\"model 'fast' not found\"}")
    assert is_model_not_found_error({"error": "model 'x' not found"})
    assert not is_model_not_found_error("ReadTimeout: timed out waiting for response")
    assert not is_model_not_found_error("")
    assert not is_model_not_found_error(None)


def test_thinker_retries_once_without_the_model():
    from vera.operator import thinker

    calls = []

    async def call_cap(name, **kw):
        calls.append((name, kw.get("model")))
        if kw.get("model") == "fast":
            return {"error": "Exception: ollama returned 404: {\"error\":\"model 'fast' not found\"}"}
        return {"text": json.dumps({"thought": "click start", "action": "click",
                                    "args": {"ref": "e1"}, "done": False})}

    class Obs:
        url = "https://x/timer.html"; title = "Timer"; text = "60 Start"; elements = []
        screenshot_b64 = ""; screenshot_path = ""

    d = asyncio.new_event_loop().run_until_complete(
        thinker.decide("verify the timer", Obs(), [], call_cap, provider="local", model="fast"))
    assert not d.get("error"), d
    assert d.get("action") == "click"
    assert [m for _, m in calls] == ["fast", None]
    assert d.get("model_dropped") == "fast"


def test_thinker_retries_when_the_404_is_raised():
    """llm.generate raised the 404 in run59; the early return skipped the retry."""
    from vera.operator import thinker

    calls = []

    async def call_cap(name, **kw):
        calls.append(kw.get("model"))
        if kw.get("model") == "fast-preview":
            raise Exception("ollama returned 404: {\"error\":\"model 'fast-preview' not found\"}")
        return {"text": json.dumps({"thought": "t", "action": "done", "args": {}, "done": True})}

    class Obs:
        url = ""; title = ""; text = ""; elements = []; screenshot_b64 = ""; screenshot_path = ""

    d = asyncio.new_event_loop().run_until_complete(
        thinker.decide("g", Obs(), [], call_cap, provider="ollama:fast-preview"))
    assert not d.get("error"), d
    assert calls == ["fast-preview", None] and d.get("model_dropped") == "fast-preview"


def test_thinker_does_not_retry_other_errors():
    from vera.operator import thinker

    calls = []

    async def call_cap(name, **kw):
        calls.append(kw.get("model"))
        return {"error": "ReadTimeout: timed out waiting for response"}

    class Obs:
        url = ""; title = ""; text = ""; elements = []; screenshot_b64 = ""; screenshot_path = ""

    d = asyncio.new_event_loop().run_until_complete(
        thinker.decide("g", Obs(), [], call_cap, provider="ollama", model="qwen2.5:7b"))
    assert d.get("error") and calls == ["qwen2.5:7b"]


def test_thinker_runs_as_the_executor_role():
    """The think must share the loop executor's runner (model + num_ctx): as
    job "code" it reloaded the model on the 12 GB card nearly every call."""
    from vera.operator import thinker

    seen = []

    async def call_cap(name, **kw):
        seen.append(kw.get("job_type"))
        return {"text": json.dumps({"thought": "t", "action": "done", "args": {}, "done": True})}

    class Obs:
        url = ""; title = ""; text = ""; elements = []; screenshot_b64 = ""; screenshot_path = ""

    asyncio.new_event_loop().run_until_complete(thinker.decide("g", Obs(), [], call_cap))
    assert seen == ["loop_executor"] and thinker.THINK_JOB_TYPE == "loop_executor"


def test_the_error_recovery_path_runs_the_heal_too():
    """Operator census run3 (2026-09-24): `_attempt_arg_recovery` rebuilds a
    failed call from its own LLM answer and healed only url/goal, so a
    `model: "fast-8b"` it invented reached Ollama. The recovery path must
    call the same heal the executor's call site does. Checked at source
    level: the module is 26k lines and imports the whole runtime."""
    import ast as _ast
    src_path = os.path.join(os.path.dirname(__file__), "..", "vera", "dag", "dag_workshop_capabilities.py")
    tree = _ast.parse(open(src_path, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_attempt_arg_recovery")
    called = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in _ast.walk(fn)
              if isinstance(c, _ast.Call)}
    assert "_heal_model_arg" in called and "_names_a_model" in called, sorted(called)
