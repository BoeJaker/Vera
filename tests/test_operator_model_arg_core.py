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
    heal_model_arg, is_model_not_found_error, model_is_served,
)

SERVED = ["jaahas/qwen3.5-uncensored:latest", "jaahas/qwen3.5-uncensored:9b",
          "qwen2.5:7b", "nomic-embed-text:latest"]


def test_the_census_case_drops_the_invented_model():
    args = {"url": "https://x/timer.html", "goal": "verify", "provider": "local", "model": "fast"}
    edits = heal_model_arg("operator.run", args, SERVED)
    assert edits and edits[0][0] == "model" and edits[0][1] == ""
    assert "'fast'" in edits[0][2] and "dropped" in edits[0][2]


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
