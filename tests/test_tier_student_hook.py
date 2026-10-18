"""loop.tier gets the System 1 student hook (four-minds request; compute-roles, 18 Oct 2026)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.critical


def _src(rel):
    return open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8").read()


def test_tier_classifier_asks_the_student_with_the_ledger_inputs():
    s = _src("vera/dag/dag_workshop_capabilities.py")
    i = s.index("async def _v7_classify_tier(")
    body = s[i:s.index("async def _v7_decide_tier(", i)]
    assert '_StudentHook.start("loop.tier", sys, prompt, inputs={"goal": goal})' in body
    assert body.index("_StudentHook.start") < body.index("_safe_ollama_generate_dw")   # before the big call
    assert "await _stu_hook.result()" in body                                            # shadow recorded


def test_the_floor_still_applies_and_the_ledger_gets_the_student():
    s = _src("vera/dag/dag_workshop_capabilities.py")
    i = s.index("async def _v7_decide_tier(")
    body = s[i:i + 4000]
    assert "suggested = heur if _v7_tier_rank(heur) >= _v7_tier_rank(llm or heur) else llm" in body
    assert '"by": _by, "student": _student_ans' in body
    assert 'by=str(tier_info.get("by") or ""), student=tier_info.get("student")' in s


def test_ledger_records_a_tier_student_as_a_shadow():
    from vera.minds import loop_ledger as LL
    sent = []
    led = LL.RunLedger("s1")
    led._send = lambda d, inputs=None: sent.append(d)
    led.tier("simple", {"heuristic": "simple", "llm": "complex", "suggested": "complex"}, model="m",
             inputs={"goal": "g"}, by="llm", student={"answer": "simple", "p": 0.9, "impl": "knn"})
    assert len(led.decisions) == 1 and len(sent) == 2       # the live decision and the student's shadow
