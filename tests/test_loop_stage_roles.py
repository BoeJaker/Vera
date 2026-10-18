"""The loop's role-less LLM stages can be placed per run (owner 10 Oct 2026: "the agentic loops
need to make much better use of the cpu nodes for llm work"). Gate, verifier, finalize, adjust,
journal, strategist, synth, deliver, prestep, fastpath, argfix and condense all queued on the one
GPU slot with no role; a run can now put any of them on a CPU node (stage_models). Unplaced, a
stage routes exactly as before."""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import role_override_core as R  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical

STAGE_OF = {
    "_v6_final_gate": "gate", "_v6_verify_step": "verifier", "_v6_finalize_step": "finalize",
    "_v6_adjust_step": "adjust", "_v6_journal_extract": "journal",
    "_v6_branch_strategist": "strategist", "_v5_synthesize_final": "synth",
    "_v6_deliver": "deliver", "_v7_generate_clarifications": "prestep",
    "_v7_assume_answers": "prestep", "_v7_prestep_info": "prestep",
    "_v7_single_cap_shortcut": "fastpath", "_attempt_arg_recovery": "argfix",
    "_v5_condense_output": "condense",
}


def test_stage_spec_parses_strings_and_dicts():
    out = R.parse_stage_models("verifier=qwen3.6:35b-a3b@cpu-246, gate=qwen3.6:35b-a3b@cpu-247,"
                               "journal=jaahas/qwen3.5-uncensored:9b@gpu-250-cpu,bogus=x@gpu,synth=")
    assert out == {"loop/verifier": {"model": "qwen3.6:35b-a3b", "node": "cpu-246"},
                   "loop/gate": {"model": "qwen3.6:35b-a3b", "node": "cpu-247"},
                   "loop/journal": {"model": "jaahas/qwen3.5-uncensored:9b", "node": "gpu-250-cpu"}}
    assert R.parse_stage_models({"finalize": {"model": "m", "node": "nowhere"}}) == \
        {"loop/finalize": {"model": "m", "node": "auto"}}
    assert R.parse_stage_models({"adjust": "m"}) == {"loop/adjust": {"model": "m", "node": "auto"}}
    assert R.parse_stage_models("") == {} and R.parse_stage_models(None) == {}
    assert set(R.STAGE_ROLES) >= {"gate", "verifier", "finalize", "journal", "synth", "deliver"}


def test_build_adds_stages_but_explicit_choices_win():
    ovr, _ = R.build(executor_model="ex", stage_models="executor=other@cpu-246,verifier=v@cpu-246")
    assert ovr["loop/executor"]["model"] == "ex"
    assert ovr["loop/verifier"] == {"model": "v", "node": "cpu-246"}
    assert R.build()[0] == {}                              # nothing placed: nothing changes


def test_every_roleless_stage_is_tagged():
    tree = ast.parse(SRC)
    seen = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in STAGE_OF:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_safe_ollama_generate_dw":
                    kw = {k.arg: k.value for k in node.keywords}
                    if "profile" in kw or "role" in kw:
                        continue
                    assert "stage_role" in kw, (fn.name, node.lineno)
                    assert ast.literal_eval(kw["stage_role"]) == STAGE_OF[fn.name]
                    seen[fn.name] = True
    assert set(seen) == set(STAGE_OF), set(STAGE_OF) - set(seen)


def test_a_stage_takes_its_role_only_when_the_run_placed_it():
    w = SRC[SRC.index("async def _safe_ollama_generate_dw("):]
    w = w[:w.index("system = _now_context_line()")]
    assert 'request_stage="", stage_role=""' in w
    assert "if stage_role and not (profile or role):" in w
    assert '"%s/%s" % (LOOP_ROUTING_PROFILE, stage_role) in (_orch.RUN_ROLE_OVERRIDES.get() or {})' in w


def test_the_run_and_route_pass_stage_models():
    sig = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    sig = sig[:sig.index("\n):")]
    assert 'stage_models:       Any  = "",' in sig
    assert "controller_node=controller_node, stage_models=stage_models)" in SRC
    assert 'v6_stage_models      = body.get("stage_models", "") or ""' in SRC
    assert "stage_models=v6_stage_models," in SRC
