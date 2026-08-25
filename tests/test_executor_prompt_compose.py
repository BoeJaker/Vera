"""The executor system prompt must not change by accident â€” Phase 4.

Phase 4 lifted the 103-line `sys = (...)` expression out of the middle of
`_v5_run_step_inner` (3,427 lines) into `_v5_compose_executor_system`. The text
was moved VERBATIM, so the extraction is correct only if what the executor
actually sends is byte-for-byte what it sent before. Sizes are not enough â€” a
reordered block or a changed separator keeps the length and changes the prompt.

`tests/golden/executor_prompt.json` was captured from the extracted function and
verified byte-identical against the pre-extraction inline expression. If a change
to prompt text is INTENDED, re-capture it in the same commit; a diff here with no
golden update means something moved that was not meant to.

Imports the app module, so it runs in-container â€” where the merge gate executes
pytest â€” and skips on a host venv, where `Vera.vera.X` resolves elsewhere. The
call-site half of this guard is pure and lives in
`test_executor_compose_callsite.py`, in the critical tier.
"""
import difflib
import inspect
import json
import pathlib

import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")

GOLDEN = pathlib.Path(__file__).parent / "golden" / "executor_prompt.json"

# Must match the inputs used to capture the golden, or the comparison is meaningless.
STEP = {"goal": "author index.html", "title": "Create file", "id": 1,
        "caps": ["code.author"]}
GOAL = "Build a habit tracker web app as a single self-contained index.html"


def _inputs():
    """The fixed input vector. Every string param gets a distinguishable value."""
    return dict(
        step=dict(STEP),
        goal=GOAL,
        _success="index.html exists with the required features",
        _chain_help="  {chain help}\n",
        _ask_help="  {ask help}\n",
        _research_hint=lambda suffix="": "web.research",
        author_note="\nAUTHOR NOTE\n",
        code_note="\nCODE NOTE\n",
        condense_note="\nCONDENSE\n",
        ctx_slice="PRIOR CONTEXT: none\n",
        edit_note="\nEDIT NOTE\n",
        exec_role_note="\nEXEC ROLE\n",
        file_access_note="\nFILE ACCESS\n",
        gen_note="\nGEN NOTE\n",
        inline_files="INLINE FILES\n",
        model_block="MODEL BLOCK\n",
        phase_guide="PHASE GUIDE",
        pkg_note="\nPKG\n",
        preview_note="\nPREVIEW\n",
        prose_note="\nPROSE\n",
        sig_block="  cap sigs\n",
        skill_prompt="SKILL PROMPT",
        terminal_note="\nTERMINAL\n",
        workdir_note="\nWORKDIR\n",
    )


def _sentinel_inputs():
    """Same shape, but each param carries its own name â€” so a dropped block shows."""
    out = {}
    for name in inspect.signature(M._v5_compose_executor_system).parameters:
        if name == "step":
            out[name] = {"goal": "SENT_step_goal", "title": "SENT_step_title",
                         "id": 1, "caps": ["code.author"]}
        elif name == "_research_hint":
            out[name] = lambda suffix="": "SENT_research"
        else:
            out[name] = "SENT_%s" % name
    return out


def test_matches_golden():
    """The composed prompt is byte-for-byte the captured one."""
    got = M._v5_compose_executor_system(**_inputs())
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))["executor"]
    if got != want:
        diff = "\n".join(list(difflib.unified_diff(
            want.splitlines(), got.splitlines(),
            fromfile="golden", tofile="composed", lineterm=""))[:60])
        pytest.fail("executor prompt drifted from golden:\n%s" % diff)


def test_every_supplied_block_reaches_the_prompt():
    """A note param that stops being interpolated must fail here, not in prod.

    This is the failure Phase 4 could plausibly introduce: a block that is
    accepted as an argument and then silently never used, so the executor
    quietly loses (say) its file-access rules with no error anywhere.
    """
    out = M._v5_compose_executor_system(**_sentinel_inputs())
    dropped = [n for n in inspect.signature(M._v5_compose_executor_system).parameters
               if n not in ("step", "_research_hint") and ("SENT_%s" % n) not in out]
    assert dropped == [], "blocks accepted but never interpolated: %s" % dropped
    assert "SENT_step_goal" in out, "the step goal must reach the prompt"
    assert "SENT_research" in out, "the research hint must reach the prompt"


def test_composition_is_pure():
    """Same inputs, same output â€” and one call must not perturb the next."""
    first = M._v5_compose_executor_system(**_inputs())
    M._v5_compose_executor_system(**_sentinel_inputs())      # different inputs between
    third = M._v5_compose_executor_system(**_inputs())
    assert first == third
    assert isinstance(first, str) and first


def test_is_a_plain_function_not_a_coroutine():
    """Composition is pure text assembly; it must never acquire I/O."""
    f = M._v5_compose_executor_system
    assert not inspect.iscoroutinefunction(f)
    src = inspect.getsource(f)
    for forbidden in ("await ", "open(", "requests.", "redis", "subprocess"):
        assert forbidden not in src, "composer must stay pure, found %r" % forbidden


def test_signature_is_keyword_only():
    """24 same-typed string params â€” positional passing would be a silent swap."""
    params = inspect.signature(M._v5_compose_executor_system).parameters
    assert params, "composer takes no parameters?"
    for name, p in params.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, \
            "%s is %s, must be keyword-only" % (name, p.kind)
