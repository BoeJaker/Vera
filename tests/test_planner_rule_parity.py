"""Both planner prompt variants must carry the same shared rules — Phase 3.

The planner has TWO prompt bodies: the full schema, and the minimal/tolerant one
that has been the DEFAULT PRIMARY since 2026-08-17 (`VERA_LOOP_MINIMAL_PLAN`).
They drift, and a rule that reaches only one is silently inert — which has now
happened twice, in opposite directions:

  * `_V7_CRITERIA_RULE` was added to the FULL branch only, so it did nothing on
    every real run (they all take the minimal path);
  * `_V7_CAP_ROUTING` reached the MINIMAL branch only, so setting
    VERA_LOOP_MINIMAL_PLAN=0 — the documented escape hatch back to the full
    primary — silently dropped cap routing and the "already checked, do not
    re-verify" rule inside it.

Neither was visible by reading either prompt alone. This composes BOTH for real,
with the generate call stubbed so nothing reaches an LLM, and asserts every rule
in `_V7_PLANNER_SHARED_RULES` appears in each.

Imports the app module, so it runs in-container — where the merge gate executes
pytest — and skips on a host venv, where `Vera.vera.X` resolves elsewhere.
"""
import asyncio
import os

import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")

CATALOG = ["code.author", "code.edit", "ide.fs.read", "exec.bash.run",
           "exec.python.run", "operator.run", "web.search", "prose.author"]
SKILLS = [{"id": "sys-exec-fileio", "description": "file io",
           "applies_to_caps": ["exec.bash.run"]}]


def _compose(minimal: bool):
    """Return the (system, prompt) the planner would actually send."""
    captured = {}

    async def _stub(prompt, system=None, **kw):
        captured.setdefault("system", system or "")
        captured.setdefault("prompt", prompt or "")
        return ('{"steps":[{"id":1,"title":"t","goal":"g","caps":["code.author"],'
                '"needs":[]}],"done_when":"d"}')

    real = M._safe_ollama_generate_dw
    prev = os.environ.get("VERA_LOOP_MINIMAL_PLAN")
    M._safe_ollama_generate_dw = _stub
    # The default flips every primary call to minimal; defeat it to reach the full body.
    os.environ["VERA_LOOP_MINIMAL_PLAN"] = "1" if minimal else "0"
    try:
        asyncio.run(M._v5_orchestrate_plan(
            "Build a habit tracker web app as a single self-contained index.html",
            CATALOG, SKILLS, max_steps=8, minimal=minimal, want_success=True,
            intent="build", sid="parity-test", stream_id="",
            model="", instance_id="", prefer_gpu=True))
    finally:
        M._safe_ollama_generate_dw = real
        if prev is None:
            os.environ.pop("VERA_LOOP_MINIMAL_PLAN", None)
        else:
            os.environ["VERA_LOOP_MINIMAL_PLAN"] = prev
    return captured.get("system", ""), captured.get("prompt", "")


@pytest.mark.parametrize("variant,minimal", [("minimal", True), ("full", False)])
def test_variant_composes_a_real_prompt(variant, minimal):
    system, prompt = _compose(minimal)
    assert len(system) > 1000, f"{variant}: system prompt looks empty ({len(system)})"
    assert prompt, f"{variant}: no user prompt composed"


@pytest.mark.parametrize("variant,minimal", [("minimal", True), ("full", False)])
def test_shared_rules_reach_this_variant(variant, minimal):
    """THE drift guard. A rule added to one branch and not the other fails here."""
    system, _ = _compose(minimal)
    missing = [rid for rid, marker in M._V7_PLANNER_SHARED_RULES if marker not in system]
    assert not missing, (
        f"{variant} planner prompt is missing shared rule(s): {missing}. "
        "Every rule in _V7_PLANNER_SHARED_RULES must reach BOTH variants — "
        "add it to the shared constant, not to one branch.")


def test_both_variants_carry_the_identical_rule_set():
    """Stated as a set comparison so the failure names the direction of the drift."""
    sys_min, _ = _compose(True)
    sys_full, _ = _compose(False)
    in_min = {rid for rid, mark in M._V7_PLANNER_SHARED_RULES if mark in sys_min}
    in_full = {rid for rid, mark in M._V7_PLANNER_SHARED_RULES if mark in sys_full}
    assert in_min == in_full, (
        f"planner rule drift — minimal-only: {sorted(in_min - in_full)}, "
        f"full-only: {sorted(in_full - in_min)}")


def test_the_shared_set_is_not_empty():
    """Guards against the guard being neutered by emptying the tuple."""
    assert len(M._V7_PLANNER_SHARED_RULES) >= 4
