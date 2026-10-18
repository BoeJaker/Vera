"""B3 (four-minds): the night's shadow judges come from the model bindings, and today's bindings give exactly the
jobs the night job used to list by hand."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import judge_core as JC  # noqa: E402
from vera.minds import shadow_plan_core as SP  # noqa: E402

pytestmark = pytest.mark.critical

MODELS = ["qwen3:4b-instruct-2507-q4_K_M", "qwen3:0.6b"]


def _old_list(cap):
    """The hand-written list the night job had until B3 (judge.py, 11 Oct)."""
    jobs = [("decide.shadow.score", {"question_id": qid, "model": model, "limit": cap, "since_hours": 48},
             f"{qid}|{model}") for model in MODELS for qid in JC.QUESTIONS]
    jobs.append(("decide.shadow.knn", {"question_id": "all", "limit": 400, "since_hours": 48}, "knn"))
    jobs.append(("decide.shadow.cards", {"question_id": "all", "limit": 400, "since_hours": 48}, "cards"))
    return jobs


def _reg(*entries):
    return {name: {"models": [dict(b, cap=name, id=name)]} for name, b in entries}


def test_the_registered_judges_give_the_old_list_exactly():
    from vera.minds import judge as J
    got = SP.plan(SP.judge_bindings(J._orch.CAPABILITY_REGISTRY), list(JC.QUESTIONS), MODELS, 40)
    old = _old_list(40)
    assert [(c, a, k) for c, _t, a, k in got][:len(old)] == old
    # the candidate matrix (17 Oct) adds the NLP-node judges after them, one job per model; the direct-entailment
    # NLI judge (18 Oct, claims against their evidence) joins them through its own binding
    assert [(c, a.get("family"), k) for c, _t, a, k in got][len(old):] == [
        ("decide.shadow.heads", None, "heads"), ("decide.shadow.nlp", "nli", "nlp-nli"),
        ("decide.shadow.nlp", "nli_direct", "nlp-nli_direct"), ("decide.shadow.nlp", "rerank", "nlp-rerank"),
        ("decide.shadow.nlp", "zeroshot", "nlp-zeroshot")]


def test_a_new_bound_student_is_judged_without_editing_the_job():
    reg = _reg(("decide.shadow.knn", {"family": "knn", "tier": 2, "questions": ["loop.step_verify"]}),
               ("decide.student.setfit", {"family": "onnx", "tier": 3, "questions": ["loop.step_verify"]}),
               ("decide.shadow.score", {"family": "ollama", "tier": 4, "questions": ["loop.step_verify"]}),
               ("nlp.ner", {"family": "nlp", "tier": 0, "questions": ["loop.step_verify"]}),          # not a judge
               ("decide.shadow.report", {"family": "nlp", "tier": 2, "questions": []}))             # answers nothing
    jobs = SP.plan(SP.judge_bindings(reg), ["loop.step_verify", "loop.done"], ["m1"], 5)
    assert [(c, k) for c, _t, _a, k in jobs] == [("decide.shadow.score", "loop.step_verify|m1"),
                                                  ("decide.shadow.knn", "knn"), ("decide.student.setfit", "setfit")]


def test_questions_outside_the_judge_set_are_never_asked():
    reg = _reg(("decide.shadow.score", {"family": "ollama", "tier": 4, "questions": ["loop.step_verify", "x.unknown"]}))
    jobs = SP.plan(SP.judge_bindings(reg), ["loop.step_verify"], ["m1", "m2"], 5)
    assert [k for *_x, k in jobs] == ["loop.step_verify|m1", "loop.step_verify|m2"]
