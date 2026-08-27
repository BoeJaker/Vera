"""A research plan must plan the write-up, not stop at retrieval.

Guards the 2026-08-27 finding (session e825c90d): with the executor's prose
steering live, a research run still finished `done` having called NEITHER
code.author NOR prose.author. The plan had only "search" and "fetch" steps and
done_when was "information ... is retrieved and extracted into readable text" -
true the moment a fetch returned. The controller wanted synthesis ("then
synthesize into readable text with prose.author") but no step carried it.

Root cause was an asymmetry between intent directives: BUILD spelled the
deliverable out in ~30 lines, RESEARCH was four lines ending at "THEN the
step(s) that use those findings" - never naming prose.author, never saying the
deliverable is a file, never warning about a retrieval-shaped done_when.

Imports the app module, so it runs in-container and skips on a host venv.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")


def _research():
    return M._v7_intent_plan_directive("research", max_steps=8)


def test_research_directive_requires_a_final_writeup_step():
    d = _research()
    assert "prose.author" in d, "the research directive must name the cap that writes it up"
    assert "FINAL STEP" in d.upper(), "the write-up must be planned as its own step"


def test_research_directive_warns_against_a_retrieval_only_criterion():
    """The exact failure: done_when satisfied by a fetch returning.

    Without this, a plan can be 'correct' and still deliver nothing, because the
    run legitimately stops the moment retrieval is done.
    """
    d = _research().lower()
    assert "retrieval alone" in d or "retrieval" in d, \
        "must address criteria that retrieval alone satisfies"
    for phrasing in ("information is retrieved", "sources are fetched"):
        assert phrasing in d, "name the wrong-criterion phrasings explicitly: %s" % phrasing


def test_research_directive_sends_summarising_to_prose_not_code():
    d = _research()
    assert "code.author" in d, "must say which cap NOT to use for the summary"
    i_code = d.index("code.author")
    assert "Do NOT" in d[max(0, i_code - 120):i_code], \
        "the code.author mention must be a prohibition, not an invitation"


def test_build_directive_is_unchanged_in_spirit():
    """Control - the fix must not disturb the BUILD shape, which is well tuned."""
    b = M._v7_intent_plan_directive("build", max_steps=8)
    assert "GOAL INTENT = BUILD" in b
    assert "prose.author" in b, "a document deliverable is still one prose.author step"
    assert "Do NOT plan research" in b


def test_mixed_intent_stays_unnarrowed():
    assert M._v7_intent_plan_directive("mixed", max_steps=8) == ""
    assert M._v7_intent_plan_directive("", max_steps=8) == ""


def test_every_known_intent_has_a_directive_except_mixed():
    for intent in M._V7_INTENTS:
        d = M._v7_intent_plan_directive(intent, max_steps=8)
        if intent == "mixed":
            assert d == ""
        else:
            assert d.strip(), "intent %r lost its directive" % intent
            assert d.startswith("GOAL INTENT ="), intent


def test_the_directive_reaches_BOTH_planner_prompt_variants():
    """Minimal is the PRIMARY planner, so a directive only in the full prompt is inert.

    Checked structurally against the source: the same _intent_directive name must
    be interpolated inside the `if minimal:` branch AND outside it.
    """
    import ast, inspect, pathlib
    src = pathlib.Path(inspect.getsourcefile(M)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_v5_orchestrate_plan")
    minimal_if = next(n for n in ast.walk(fn)
                      if isinstance(n, ast.If)
                      and (ast.get_source_segment(src, n.test) or "").strip() == "minimal")
    lo, hi = minimal_if.body[0].lineno, minimal_if.body[-1].end_lineno
    uses = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Name) and n.id == "_intent_directive"
            and isinstance(n.ctx, ast.Load)]
    assert any(lo <= ln <= hi for ln in uses), "not used in the minimal prompt"
    assert any(not (lo <= ln <= hi) for ln in uses), "not used in the full prompt"
