"""decide.calibration: every student's numbers per question and implementation, and FM-2.5's revert triggers
(four-minds FM-11)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import calibration_core as CC  # noqa: E402

pytestmark = pytest.mark.critical
Q = "loop.step_verify"


def _sh(answer, p, live, gold, impl="llm:qwen3:4b", would_take=False, q=Q, f="d"):
    return {"question_id": q, "impl": impl, "answer": answer, "p": p, "live": live, "gold": gold,
            "would_take": would_take, "for": f}


def test_an_implementation_against_gold_with_precision_at_tau_and_the_cosign():
    rows = [_sh("met", 0.97, "met", "met")] * 18 + [_sh("met", 0.96, "met", "not_met")] * 2 + [
        _sh("not_met", 0.1, "met", None, would_take=True), _sh("met", 0.99, "met", "met", would_take=True)]
    s = CC.implementation_stats(Q, rows, tau=0.95)
    assert s["n"] == 22 and s["vs_gold"]["n"] == 21 and s["vs_gold"]["precision_at_tau"] == pytest.approx(0.905, abs=0.001)
    assert s["cosign"] == {"n": 2, "agrees_with_live": 0.5, "right_vs_gold": 1.0}
    tier = [_sh("simple", 0.9, "simple", "simple", q="loop.tier")] * 3 + [_sh("complex", 0.96, "simple", "simple", q="loop.tier")]
    t = CC.implementation_stats("loop.tier", tier, tau=0.95)
    assert t["vs_gold"]["accuracy"] == 0.75 and t["vs_gold"]["precision_at_tau"] == 0.0
    assert CC.implementation_stats(Q, []) == {"n": 0}


def test_escalation_counts_handovers_only_in_student_runs_and_leaves_out_cosign():
    live = [{"question_id": Q, "run_id": "r1", "decider": "student", "decision_id": "a"},
            {"question_id": Q, "run_id": "r1", "decider": "llm", "decision_id": "b"},
            {"question_id": Q, "run_id": "r1", "decider": "llm", "decision_id": "c"},
            {"question_id": Q, "run_id": "r2", "decider": "llm", "decision_id": "d"}]     # no student in r2
    assert CC.escalation(live) == {Q: {"took": 1, "handed": 2, "rate": 0.667}}
    assert CC.escalation(live, cosigned={"c"}) == {Q: {"took": 1, "handed": 1, "rate": 0.5}}
    assert CC.escalation(live[1:]) == {}
    intel = [{"question_id": "intel.page_genre", "run_id": "x", "decider": "student", "mode": "rule", "decision_id": "i"}]
    assert CC.escalation(intel) == {}                                         # a heuristic, not a cascade


def test_drift_and_the_alerts_need_enough_rows():
    assert CC.drift(["a"] * 5 + ["b"] * 5, ["a"] * 10) == 0.5 and CC.drift([], ["a"]) is None
    good = {"vs_gold": {"n": 30, "precision_at_tau": 0.99, "ece": 0.02}}
    worse = {"vs_gold": {"n": 30, "precision_at_tau": 0.95, "ece": 0.08}}
    al = CC.alerts(Q, "llm:x", worse, good)
    assert any("precision at tau fell" in a for a in al) and any("ECE" in a for a in al)
    assert CC.alerts(Q, "llm:x", {"vs_gold": dict(worse["vs_gold"], n=5)}, good) == []          # too few rows
    assert CC.alerts(Q, "", {}, {}, {"took": 5, "handed": 20, "rate": 0.8}) == [f"{Q}: escalation 80% > 60%"]
    assert CC.alerts(Q, "", {}, {}, {"took": 1, "handed": 4, "rate": 0.8}) == []
    assert "drifted" in CC.alerts(Q, "", {}, {}, None, 0.4)[0]


def test_the_whole_report():
    cur = [_sh("met", 0.97, "met", "met", f=f"d{i}") for i in range(4)] + [_sh("met", 0.9, None, None, f="gone")]
    prev = [_sh("met", 0.97, "met", "met")]
    live_cur = [{"question_id": Q, "decision_id": f"d{i}", "run_id": "r", "decider": "llm", "answer": "met",
                 "stored": i != 0} for i in range(4)]
    live_prev = [{"question_id": Q, "decision_id": "p", "run_id": "r0", "decider": "llm", "answer": "not_met", "stored": True}]
    rep = CC.report(cur, prev, live_cur, live_prev)
    q = rep["questions"][Q]
    assert q["live"] == 4 and q["drop_rate"] == 0.25 and q["orphan_rate"] == 0.2 and q["drift"] == 1.0
    assert q["implementations"]["llm:qwen3:4b"]["current"]["n"] == 4          # the orphan is left out
    assert any("drifted" in a for a in rep["alerts"])
    src = open(os.path.join(ROOT, "vera", "minds", "calibration.py"), encoding="utf-8").read()
    assert 'getattr(_orch, "_IS_WORKER", False)' in src and "lpush(KEY_HISTORY" in src
    assert 'os.path.join(_here, "minds/calibration.py"),' in open(
        os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()


def test_the_zero_shot_intent_shadow_names_its_live_decision():
    from vera.minds import loop_ledger, loop_decisions_core as LD
    rows = []
    rl = loop_ledger.RunLedger("sess", origin="census", sink=rows.append)
    rl.intent("build", {"heuristic": "build", "llm": "build"}, model="big")
    rl.intent_shadow({"zeroshot": "research", "used": "build", "scores": {"research": 0.9}})
    live, shadow = rows[0], rows[-1]
    assert shadow["mode"] == "shadow" and shadow["detail"]["for"] == live["decision_id"]
    assert "for" not in LD.intent_shadow_decision("s", {"zeroshot": "build"})["detail"]       # no live: no link
