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
    assert JC.user_message("intel.signal_relevant", {"goal": "g"}) is None      # no judge for it


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


STATES = {
    "loop.step_done": {"step": "fetch", "step_goal": "get the page", "result": "got 3 pages"},
    "loop.prestep_needed": {"goal": "g", "step": "s", "step_goal": "find prices", "known": "none"},
    "loop.intent": {"goal": "write a timer app"},
    "loop.tier": {"goal": "write a timer app"},
    "loop.controller_action": {"goal": "g", "done_when": "x", "after_step": "s1", "result": "r",
                               "done": [{"title": "s1", "ok": True, "met": True}], "queue": ["s2"], "steps_left": 4},
}


def test_every_loop_question_has_a_judge_prompt():
    for qid, st in STATES.items():
        assert qid in JC.QUESTIONS, qid
        m = JC.user_message(qid, st)
        assert m and len(m) < 6000, qid
    assert JC.user_message("loop.intent", {}) is None and JC.user_message("loop.controller_action", {}) is None
    assert "s1 (met)" in JC.user_message("loop.controller_action", STATES["loop.controller_action"])


def test_choice_questions_answer_with_letters():
    probs, mass = JC.label_probs(_tops(A=0.1, B=0.7, C=0.1, D=0.05), ["a", "b", "c", "d"])
    assert JC.verdict("loop.intent", probs) == ("research", round(0.7 / 0.95, 4))
    assert JC.verdict("loop.tier", probs)[0] == "simple" and JC.verdict("loop.controller_action", probs)[0] == "replan"
    assert JC.positive_label("loop.intent") is None and JC.positive_label("loop.step_done") == "end"
    assert JC.agreement([("build", "build", 0.9), ("build", "mixed", 0.6)], None) == {"n": 2, "agreement": 0.5, "auc": None}


def test_live_answers_are_labels_the_judge_can_give():
    assert set(JC.QUESTIONS["loop.intent"]["labels"].values()) == set(LD.INTENT_LABELS)
    assert set(JC.QUESTIONS["loop.tier"]["labels"].values()) == set(LD.TIER_LABELS)
    assert set(JC.QUESTIONS["loop.controller_action"]["labels"].values()) == set(LD.CONTROLLER_LABELS)
    assert set(JC.QUESTIONS["loop.prestep_needed"]["labels"].values()) == set(LD.PRESTEP_LABELS)
    assert set(JC.QUESTIONS["loop.step_done"]["labels"].values()) == set(LD.STEP_DONE_LABELS)


def test_the_all_report_calls_the_capability_by_keyword():
    """A @capability wrapper takes keyword arguments only (a positional call 500'd, 2026-10-09)."""
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "cap_decide_shadow_report(question_id=qid, days=days)" in src
    assert "cap_decide_shadow_report(qid," not in src


def test_true_label_from_the_outcome():
    assert JC.true_label("loop.step_verify", "met", "correct") == "met"
    assert JC.true_label("loop.step_verify", "met", "wrong") == "not_met"
    assert JC.true_label("loop.step_verify", "met", "interrupted") is None
    assert JC.true_label("loop.intent", "build", "correct") is None          # choice questions: no inversion
    assert JC.true_label("loop.step_verify", "maybe", "correct") is None


def test_calibration_precision_at_confidence():
    rows = [(0.95, "met"), (0.9, "met"), (0.85, "not_met"), (0.2, "not_met"), (0.05, "not_met"), (0.6, "met")]
    c = JC.calibration(rows, "met", thresholds=(0.5, 0.9))
    assert c["n"] == 6 and c["accuracy"] == round(5 / 6, 3)
    assert c["at_confidence"]["0.9"] == {"coverage": round(3 / 6, 3), "precision": 1.0}
    assert c["at_confidence"]["0.5"]["coverage"] == 1.0
    assert 0 <= c["ece"] <= 1
    assert JC.calibration([], "met") == {"n": 0}


def test_knn_vote_weights_neighbours_and_skips_its_own_run():
    pool = [{"vec": [1.0, 0.0], "answer": "met", "run": "a"}, {"vec": [0.9, 0.1], "answer": "met", "run": "b"},
            {"vec": [0.8, 0.2], "answer": "not_met", "run": "c"}, {"vec": [0.0, 1.0], "answer": "not_met", "run": "d"},
            {"vec": [0.1, 0.9], "answer": "not_met", "run": "e"}, {"vec": [0.95, 0.05], "answer": "met", "run": "self"}]
    ans, p, n = JC.knn_vote("loop.step_verify", [1.0, 0.0], pool, exclude_run="self", k=3, min_neighbours=5)
    assert ans == "met" and n == 5 and 0.5 < p < 1.0
    assert JC.knn_vote("loop.step_verify", [1.0, 0.0], pool[:3], min_neighbours=5)[0] is None
    ans, p, _ = JC.knn_vote("loop.intent", [1.0, 0.0],
                            [{"vec": [1.0, 0.0], "answer": "build", "run": str(i)} for i in range(5)], min_neighbours=5)
    assert ans == "build" and p == 1.0


def test_the_night_job_also_queues_the_knn_judge():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "SP.plan(SP.judge_bindings(_orch.CAPABILITY_REGISTRY)" in src     # B3: from the bindings (test_shadow_plan)
    assert "cap_decide_shadow_knn(question_id=q," in src


def test_the_nightly_judge_runs_each_configured_model():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert 'DEFAULT_MODELS = "qwen3:4b-instruct-2507-q4_K_M,qwen3:0.6b"' in src
    assert "_models(), cap)" in src                                     # the planner asks per model (test_shadow_plan)


def test_night_triggers_run_on_prod_only_and_mark_the_night_after_success():
    for rel in (("vera", "minds", "judge.py"), ("vera", "minds", "tool_shortlist.py")):
        src = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
        body = src[src.index("async def _night_job"):src.index("schedule(_night_job")]
        assert 'if getattr(_orch, "_IS_WORKER", False):' in body, rel
        assert "nx=True" not in body, rel                      # the key is no longer taken before the work
        assert body.index("await r.get(KEY_NIGHT + night)") < body.index("await r.set(KEY_NIGHT + night"), rel
    judge = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "if not failed:" in judge and "id=jid" in judge


def test_the_raw_helper_never_collides_with_a_capability_argument():
    """background.cap.enqueue takes name=...: with _raw(name, **kw) the night trigger raised 'got multiple values for
    argument name' on every run (2026-10-09, swallowed until logged). The helper's capability name is positional-only."""
    for rel in (("vera", "minds", "judge.py"), ("vera", "minds", "tool_shortlist.py"), ("vera", "minds", "sense_capabilities.py")):
        src = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
        line = next(l for l in src.splitlines() if l.startswith("async def _raw("))
        assert ", /" in line, (rel, line)


def test_choice_questions_are_scored_against_gold():
    rows = [("continue", 0.95, "continue"), ("stop", 0.92, "continue"), ("stop", 0.6, "stop"),
            ("replan", 0.4, "stop"), ("stop", None, "stop"), ("stop", 0.9, None)]
    c = JC.choice_calibration(rows)
    assert c["n"] == 4 and c["accuracy"] == 0.5                        # rows without p or gold drop out
    assert c["at_confidence"]["0.9"] == {"coverage": 0.5, "precision": 0.5}
    assert c["at_confidence"]["0.5"] == {"coverage": 0.75, "precision": 0.667}
    assert 0.0 <= c["ece"] <= 1.0 and JC.choice_calibration([]) == {"n": 0}


def test_the_live_baseline_is_the_bar():
    assert JC.live_accuracy([("end", "end"), ("end", "continue"), ("end", None), ("continue", "continue")]) == {
        "n": 3, "accuracy": 0.667}
    assert JC.live_accuracy([("a", None)]) == {"n": 0}


def test_the_report_takes_gold_from_the_export_rule_for_every_question():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    body = src[src.index("async def cap_decide_shadow_report"):src.index("_DDL_VEC = (")]
    assert 'EX.settle(live, outs, "medium")' in body and "JC.choice_calibration(" in body
    assert "JC.avoids_known_wrong(" in body
    assert '"live_vs_outcomes": JC.live_accuracy(' in body and '"live": {qid:' in body


def test_known_wrong_answers_count_against_the_live_model_and_for_a_shadow_that_avoids_them():
    assert JC.live_accuracy([("stop", "stop"), ("continue", None, "continue"), ("stop", None, None)]) == {
        "n": 2, "accuracy": 0.5}
    assert JC.avoids_known_wrong([("replan", "continue"), ("continue", "continue"), ("x", None)]) == {
        "n": 2, "rate": 0.5}
    assert JC.avoids_known_wrong([]) == {"n": 0}
