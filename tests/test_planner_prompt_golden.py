"""The composed planner prompts must not change by accident — Phase 1.

This is the acceptance bar for the rule-registry extraction: moving rule text
into `loop_prompt_rules.py` is behaviour-neutral ONLY if what the planner
actually sends is byte-for-byte what it sent before. Sizes are not enough — a
reordering or a changed separator keeps the length and changes the prompt.

`tests/golden/planner_prompts.json` was captured from the pre-extraction code
with fixed inputs. If a change to prompt text is INTENDED, re-capture it in the
same commit; a diff here with no golden update means something moved that was
not meant to.

Imports the app module, so it runs in-container - where the merge gate executes
pytest - and skips on a host venv, where `Vera.vera.X` resolves elsewhere.
"""
import asyncio
import difflib
import json
import os
import pathlib

import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")

GOLDEN = pathlib.Path(__file__).parent / "golden" / "planner_prompts.json"

# Must match the inputs used to capture the golden, or the comparison is meaningless.
CATALOG = ["code.author", "code.edit", "ide.fs.read", "exec.bash.run",
           "exec.python.run", "operator.run", "web.search", "prose.author"]
SKILLS = [{"id": "sys-exec-fileio", "description": "file io",
           "applies_to_caps": ["exec.bash.run"]}]
GOAL = "Build a habit tracker web app as a single self-contained index.html"


def _compose(minimal: bool):
    captured = {}

    async def _stub(prompt, system=None, **kw):
        captured.setdefault("system", system or "")
        captured.setdefault("prompt", prompt or "")
        return ('{"steps":[{"id":1,"title":"t","goal":"g","caps":["code.author"],'
                '"needs":[]}],"done_when":"d"}')

    real = M._safe_ollama_generate_dw
    prev = os.environ.get("VERA_LOOP_MINIMAL_PLAN")
    M._safe_ollama_generate_dw = _stub
    os.environ["VERA_LOOP_MINIMAL_PLAN"] = "1" if minimal else "0"
    try:
        asyncio.run(M._v5_orchestrate_plan(
            GOAL, CATALOG, SKILLS, max_steps=8, minimal=minimal, want_success=True,
            intent="build", sid="golden", stream_id="",
            model="", instance_id="", prefer_gpu=True))
    finally:
        M._safe_ollama_generate_dw = real
        if prev is None:
            os.environ.pop("VERA_LOOP_MINIMAL_PLAN", None)
        else:
            os.environ["VERA_LOOP_MINIMAL_PLAN"] = prev
    return captured


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _report(name, want, got):
    d = "\n".join(list(difflib.unified_diff(
        want.splitlines(), got.splitlines(),
        fromfile=f"golden/{name}", tofile=f"composed/{name}", lineterm=""))[:40])
    return (f"{name} changed ({len(want)} -> {len(got)} chars).\n"
            f"If intended, re-capture tests/golden/planner_prompts.json in the same "
            f"commit.\nFirst differences:\n{d}")


@pytest.mark.parametrize("variant,minimal", [("minimal", True), ("full", False)])
def test_composed_system_prompt_is_byte_identical(variant, minimal):
    want = _golden()[variant]["system"]
    got = _compose(minimal)["system"]
    assert got == want, _report(f"{variant}.system", want, got)


@pytest.mark.parametrize("variant,minimal", [("minimal", True), ("full", False)])
def test_composed_user_prompt_is_byte_identical(variant, minimal):
    want = _golden()[variant]["prompt"]
    got = _compose(minimal)["prompt"]
    assert got == want, _report(f"{variant}.prompt", want, got)


def test_shared_rule_texts_are_byte_identical():
    """Pins the rule bodies themselves, so an extraction that 'tidies' whitespace
    fails here rather than silently altering every prompt that embeds them."""
    want = _golden()["_rules"]
    for rid, const in (("cap_routing", "_V7_CAP_ROUTING"),
                       ("criteria_settleable", "_V7_CRITERIA_RULE")):
        assert getattr(M, const) == want[rid], _report(rid, want[rid], getattr(M, const))


def test_golden_is_present_and_substantial():
    """A truncated or missing golden must fail loudly, not pass vacuously."""
    g = _golden()
    assert len(g["minimal"]["system"]) > 5000
    assert len(g["full"]["system"]) > 5000
