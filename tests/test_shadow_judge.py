"""A small model as a nightly System 1 shadow judge (four-minds FM-2)."""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import judge_core as JC, ledger_core as LC, loop_decisions_core as LD  # noqa: E402

pytestmark = pytest.mark.critical


def _tops(**lp):
    return [{"token": k, "logprob": math.log(v)} for k, v in lp.items()]


def test_label_probabilities_renormalise_over_the_labels():
    probs, mass = JC.label_probs(_tops(Yes=0.6, No=0.2, **{"<think>": 0.1, " yes": 0.05}), ["yes", "no"])
    assert mass == pytest.approx(0.85)
    assert probs["yes"] == pytest.approx(0.65 / 0.85) and probs["no"] == pytest.approx(0.2 / 0.85)
    assert JC.label_probs(_tops(maybe=0.9), ["yes", "no"]) == ({}, 0.0)


def test_verdict_and_the_positive_probability():
    assert JC.verdict("loop.step_verify", {"yes": 0.3, "no": 0.7}) == ("not_met", 0.3)
    assert JC.verdict("loop.step_verify", {"yes": 0.9, "no": 0.1}) == ("met", 0.9)
    assert JC.verdict("loop.step_verify", {}) == (None, None)


def test_the_question_needs_its_inputs():
    m = JC.user_message("loop.step_verify", {"success": "timer.html exists", "result": "wrote timer.html"})
    assert "timer.html exists" in m and "wrote timer.html" in m
    assert JC.user_message("loop.step_verify", {"success": "x"}) is None
    assert JC.user_message("loop.controller_action", {"goal": "g"}) is None


def test_the_shadow_row_is_valid_and_linked():
    live = LD.verify_decision("sess-9", 2, True, 1, model="big")
    sh = JC.shadow_decision(live, "not_met", 0.31, "llm:qwen3.5:2b", mass=0.99, ms=3200)
    assert LC.check_decision(sh) == []
    assert sh["mode"] == "shadow" and sh["question_id"] == live["question_id"]
    assert sh["detail"]["for"] == live["decision_id"] and sh["state_ref"] == live["state_ref"]
    assert sh["implementation"] == "llm:qwen3.5:2b" and sh["p"] == 0.31 and sh["run_id"] == "sess-9"


def test_night_window_wraps_midnight():
    assert JC.in_window(23) and JC.in_window(2) and not JC.in_window(6) and not JC.in_window(14)


def test_agreement_and_auc():
    pairs = [("met", "met", 0.9), ("met", "met", 0.8), ("not_met", "not_met", 0.2), ("not_met", "met", 0.6)]
    a = JC.agreement(pairs, "met")
    assert a["n"] == 4 and a["agreement"] == 0.75 and a["auc"] == 1.0
    assert JC.agreement([], "met") == {"n": 0}


def test_ollama_generate_can_return_label_logprobs():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert "logprobs: int = 0) -> str:" in src
    assert 'body["top_logprobs"] = max(1, min(20, int(logprobs)))' in src
    assert 'meta_out["logprobs"] = lpbuf' in src
    assert 'os.path.join(_here, "minds/judge.py"),' in src


def test_the_judge_is_background_work_queued_nightly():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert 'JOB_TYPE = "idle_decide"' in src and "background.cap.enqueue" in src
    assert "if await _gate_busy():" in src
