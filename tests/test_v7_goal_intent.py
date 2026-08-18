"""v7 goal-intent classification + intent-driven plan shape.

The KIND of a goal (build / research / action / mixed) is orthogonal to its SIZE
(the tier). Intent drives the plan SHAPE: a BUILD goal — producible directly from
the model's knowledge (a small app, a script, a doc from a spec) — must be planned
as DIRECT AUTHORING (code.author/prose.author per file), NOT as a research pipeline
(search an example → fetch → replicate), which is what turned "create a pomodoro
app" into empty files + wasted cycles.

These tests pin the deterministic pieces: the heuristic classifier, the intent
plan-directive text, and that _v5_orchestrate_plan actually injects the BUILD
directive + the canonical cap-routing into its prompt (asserted by capturing the
system prompt handed to the LLM — no real model call).

Imports the monolith module, so it runs in-container (see test_loop_cancel.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


# ── heuristic classifier ─────────────────────────────────────────────────────
def test_build_goals_classified_build():
    for g in ("create a pomodoro app", "write a snake game in html",
              "build a calculator", "make a landing page", "draft a README for this repo"):
        assert W._v7_intent_heuristic(g) == "build", g


def test_research_goals_classified_research_or_mixed():
    assert W._v7_intent_heuristic("look up the latest AI news") == "research"
    assert W._v7_intent_heuristic("research current GPU prices") == "research"
    # build verb + external-info word ⇒ mixed (needs lookup THEN build)
    assert W._v7_intent_heuristic("look up the latest React docs then build a demo") == "mixed"


def test_action_goals_classified_action():
    assert W._v7_intent_heuristic("deploy the app to the server") == "action"
    assert W._v7_intent_heuristic("run the test suite and fix the failures") in ("action", "mixed")


def test_ambiguous_defaults_to_mixed_not_build():
    # No clear build noun+verb, no research/action words → mixed (don't wrongly
    # narrow the planner to direct-authoring on an unclear goal).
    assert W._v7_intent_heuristic("help me with my project") == "mixed"
    assert W._v7_intent_heuristic("") == "mixed"


# ── intent plan-directive text ───────────────────────────────────────────────
def test_build_directive_forbids_research_steps():
    d = W._v7_intent_plan_directive("build")
    assert "BUILD" in d
    assert "code.author" in d and "prose.author" in d
    # It must actively forbid research/example-hunting.
    assert "do NOT plan research" in d or "do NOT add" in d
    assert "web.search" in d  # named as something NOT to add


def test_research_and_action_directives_present_mixed_empty():
    assert "RESEARCH" in W._v7_intent_plan_directive("research")
    assert "ACTION" in W._v7_intent_plan_directive("action")
    assert W._v7_intent_plan_directive("mixed") == ""   # no narrowing


# ── the plan prompt actually carries the directive + canonical routing ───────
def _capture_plan_prompt(monkeypatch, intent):
    captured = {}

    async def _fake_gen(prompt, system="", **kw):
        captured["system"] = system
        captured["prompt"] = prompt
        return '{"steps": []}'

    monkeypatch.setattr(W, "_safe_ollama_generate_dw", _fake_gen)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(W, "emit_event", _noop)

    asyncio.run(W._v5_orchestrate_plan(
        "create a pomodoro app", ["code.author", "prose.author", "web.search", "exec.bash.run"],
        [], {}, model="m", instance_id="i", prefer_gpu=False, max_steps=6,
        want_success=True, intent=intent))
    return captured


def test_build_plan_prompt_injects_directive_and_routing(monkeypatch):
    cap = _capture_plan_prompt(monkeypatch, "build")
    s = cap.get("system", "")
    assert "GOAL INTENT = BUILD" in s, "build directive not injected into planner prompt"
    assert "CAPABILITY ROUTING" in s, "canonical cap-routing not injected"
    # The canonical routing must state the code/prose split.
    assert "code.author" in s and "prose.author" in s


def test_mixed_plan_prompt_has_routing_but_no_build_narrowing(monkeypatch):
    cap = _capture_plan_prompt(monkeypatch, "mixed")
    s = cap.get("system", "")
    assert "GOAL INTENT = BUILD" not in s
    assert "CAPABILITY ROUTING" in s   # routing is always present
