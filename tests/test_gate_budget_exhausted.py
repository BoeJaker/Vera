"""A run that used its whole step budget is still judged by the final gate once, so it
cannot end 'complete' unchecked (live 2026-10-01: max_steps=1, three-part goal, no gate)."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_budget_exhausted_runs_get_one_verdict_before_the_end():
    loop = SRC.index("while enable_final_gate and executed < hard_cap and _gate_rounds < _MAX_GATE_ROUNDS:")
    once = SRC.index("if enable_final_gate and _gate_rounds == 0 and results and executed >= hard_cap:", loop)
    unmet = SRC.index("_unmet = _gate_finish_core.unmet(_gate_last, _gate_ran_after)", once)
    block = SRC[once:unmet]
    assert "steps_left=0" in block and "_gate_last = _gb" in block
    assert '"budget_exhausted": True' in block and '"follow_up": []' in block
