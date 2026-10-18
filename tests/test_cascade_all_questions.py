"""FM-2.5 for every question at once (owner, 17 Oct): a switch per question, the student and its threshold chosen
from the shadow report, one combined arm - and the two questions with their own settings unchanged."""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import cascade_core as CC  # noqa: E402
from vera.minds import judge_core as JC  # noqa: E402
from vera.minds import student as ST  # noqa: E402
from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def _impl(n, at):
    return {"n": n, "vs_outcomes": {"n": n, "accuracy": 0.8,
                                    "at_confidence": {t: {"precision": p, "coverage": c} for t, (p, c) in at.items()}}}


# the 17 Oct shadow report, trimmed to what the plan reads
REPORT = {
    "live": {"loop.tier": {"n": 367, "accuracy": 0.853}, "loop.step_verify": {"n": 100, "accuracy": 1.0},
             "loop.step_done": {"n": 327, "accuracy": 0.768}, "loop.intent": {"n": 0}},
    "questions": {
        "loop.tier": {"knn:minilm": _impl(313, {"0.5": (0.87, 1.0), "0.7": (0.93, 0.86), "0.8": (0.97, 0.751)}),
                      "llm:qwen3:4b-instruct-2507-q4_K_M": _impl(195, {"0.8": (0.708, 0.79)})},
        "loop.step_verify": {"llm:qwen3:4b-instruct-2507-q4_K_M": _impl(43, {"0.5": (0.977, 1.0)}),
                             "knn:minilm": _impl(100, {"0.5": (0.26, 1.0)})},
        "loop.step_done": {"llm:qwen3:4b-instruct-2507-q4_K_M": _impl(18, {"0.8": (0.882, 0.944)}),
                           "knn:minilm": _impl(327, {"0.5": (0.768, 1.0)}),
                           "card:minilm": _impl(327, {"0.8": (0.854, 0.125)})},
        "loop.intent": {"knn:minilm": _impl(50, {"0.5": (0.9, 1.0)})},
    }}


def test_the_plan_from_the_17_oct_report():
    plan = CC.choose(REPORT, "qwen3:1.7b")
    assert plan["loop.tier"]["eligible"] and plan["loop.tier"]["impl"] == "knn:minilm"
    assert plan["loop.tier"]["tau"] == 0.5 and plan["loop.tier"]["coverage"] == 1.0   # 0.87 >= 0.853 already at 0.5
    assert not plan["loop.step_verify"]["eligible"]           # the live verifier is perfect; nothing beats it
    # step_done: kNN only equals live at 0.5 (eligible: precision >= live), the 4B has too few outcomes, the card judge
    # (not runnable live) is ignored
    assert plan["loop.step_done"]["impl"] == "knn:minilm" and plan["loop.step_done"]["eligible"]
    assert not plan["loop.intent"]["eligible"] and "live outcomes" in plan["loop.intent"]["reason"]
    assert CC.arm_minds(plan, ST.FEATURE) == {"decide.student.tier": "first", "decide.student.step_done": "first"}


def test_only_runnable_students_count_and_the_in_slot_llm_is_one():
    assert CC.runnable("knn:minilm", "qwen3:1.7b") and CC.runnable("llm:qwen3:1.7b", "qwen3:1.7b")
    assert not CC.runnable("llm:qwen3:1.7b@cpu-247", "qwen3:1.7b") and not CC.runnable("llm:qwen3:4b", "qwen3:1.7b")
    # any matrix winner that answers in milliseconds is promotable (17 Oct): cards, the NLP-node judges, trained heads
    for impl in ("card:minilm", "zeroshot:deberta-base", "nli:deberta-large", "rerank:msmarco-minilm", "onnx:step_done"):
        assert CC.runnable(impl, "qwen3:1.7b")


def test_every_question_has_a_switch_off_by_default():
    for q, fid in ST.FEATURE.items():
        assert q in JC.QUESTIONS and SB.REGISTRY[fid].default == "off"
    for q in ("loop.step_done", "loop.prestep_needed", "loop.intent", "loop.tier"):
        assert SB.REGISTRY[ST.FEATURE[q]].modes == ("off", "shadow", "first")


def test_planned_questions_follow_the_plan_and_the_legacy_two_do_not(monkeypatch):
    monkeypatch.setattr(ST, "PLAN", {"plan": CC.choose(REPORT, "qwen3:1.7b"), "at": 1e18})
    assert ST.impl_for("loop.tier") == "knn" and ST.tau_for("loop.tier") == 0.5
    assert ST.impl_for("loop.step_verify") == "llm" and ST.tau_for("loop.step_verify") == ST.tau()
    assert set(ST.allowed("loop.tier")) == {"single", "simple", "complex", "strategic"}
    assert ST.decides("loop.tier", {"answer": "simple", "p": 0.6})          # chosen label, p 0.6 >= tau 0.5
    # not eligible: 'first' is a shadow, whatever the confidence
    assert not ST.decides("loop.intent", {"answer": "build", "p": 0.99})
    assert not ST.decides("loop.prestep_needed", {"answer": "needed", "p": 0.99})


def test_a_knn_student_without_a_nightly_index_defers_to_the_big_model(monkeypatch):
    monkeypatch.setattr(ST, "PLAN", {"plan": CC.choose(REPORT, "qwen3:1.7b"), "at": 1e18})

    class _R:
        async def get(self, k):
            return None
    import types
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration",
                        types.SimpleNamespace(REDIS=_R(), CAPABILITY_REGISTRY={}))
    out = asyncio.run(ST.ask("loop.tier", "sys", "goal: build me a site", inputs={"goal": "build me a site"}))
    assert out is None


def test_the_hook_passes_the_decisions_inputs_through():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "inputs: Optional[Dict[str, Any]] = None) -> \"_StudentHook\":" in src
    assert "prompt,\n                                inputs=inputs)" in src
    j = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "await r_.set(KNN_VERSION_KEY + question_id, version" in j and "async def knn_rows(" in j


def test_any_matrix_winner_routes_to_its_family(monkeypatch):
    plan = {"loop.tier": {"eligible": True, "impl": "zeroshot:deberta-base", "tau": 0.7},
            "loop.intent": {"eligible": True, "impl": "card:minilm", "tau": 0.8},
            "loop.step_done": {"eligible": True, "impl": "onnx:student-loop-step-done", "tau": 0.6},
            "loop.prestep_needed": {"eligible": False, "impl": "nli:deberta-large"}}
    monkeypatch.setattr(ST, "PLAN", {"plan": plan, "at": 1e18})
    assert ST.impl_for("loop.tier") == "nlp:zeroshot" and ST.impl_for("loop.intent") == "card"
    assert ST.impl_for("loop.step_done") == "onnx:student-loop-step-done"
    assert ST.impl_for("loop.prestep_needed") == "nlp:nli" and ST.impl_for("loop.step_verify") == "llm"


def test_a_zero_shot_winner_answers_live(monkeypatch):
    import types
    monkeypatch.setattr(ST, "PLAN", {"plan": {"loop.tier": {"eligible": True, "impl": "zeroshot:deberta-base",
                                                            "tau": 0.7}}, "at": 1e18})

    async def _nlp_probs(fam, text, cards):
        assert fam == "zeroshot" and text.startswith("Goal: build me a site")
        return {"single": 0.05, "simple": 0.8, "complex": 0.1, "strategic": 0.05}
    monkeypatch.setitem(sys.modules, "judge", types.SimpleNamespace(_nlp_probs=_nlp_probs))
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration",
                        types.SimpleNamespace(REDIS=None, CAPABILITY_REGISTRY={}))
    out = asyncio.run(ST.ask("loop.tier", "sys", "prompt", inputs={"goal": "build me a site"}))
    assert out["answer"] == "simple" and out["impl"] == "zeroshot:deberta-base" and out["p"] == 0.8
    assert ST.decides("loop.tier", out)                                   # 0.8 >= the plan's 0.7
