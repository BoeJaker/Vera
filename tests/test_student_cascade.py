"""The System 1 student on live step verdicts (four-minds FM-2.5): same evidence as the big model, who decided
recorded on every verdict, the student shadowed or first per the switchboard."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import (judge_core as JC, loop_decisions_core as LD, loop_ledger,  # noqa: E402
                        student as ST, switchboard_core as SB, training_state_core as TS)

pytestmark = pytest.mark.critical

LIVE_SYS = ("You judge whether ONE step of an agentic run met its SUCCESS CRITERION. Judge STRICTLY.\n"
            'Respond ONLY with JSON: {"met":true,"reason":"<one sentence quoting the evidence>"}')


def test_the_student_gets_the_live_rules_and_evidence_in_one_word():
    s = JC.student_system(LIVE_SYS)
    assert "Judge STRICTLY" in s and "JSON" not in s and s.endswith(JC.STUDENT_VERIFY_ANSWER)
    state = {"evidence": "SUCCESS CRITERION: x\nTOOL CALLS ...", "evidence_system": LIVE_SYS,
             "success": "x", "result": "summary only"}
    assert JC.user_message("loop.step_verify", state) == state["evidence"]
    assert JC.system_for("loop.step_verify", state) == s
    old = {"success": "x", "result": "summary only"}                    # rows stored before the evidence
    assert "Step result: summary only" in JC.user_message("loop.step_verify", old)
    assert JC.system_for("loop.step_verify", old) == JC.QUESTIONS["loop.step_verify"]["system"]


def test_confidence_and_when_a_student_answer_stands():
    assert JC.confidence("loop.step_verify", "met", 0.97) == pytest.approx(0.97)
    assert JC.confidence("loop.step_verify", "not_met", 0.02) == pytest.approx(0.98)
    assert JC.confidence("loop.tier", "simple", 0.8) == pytest.approx(0.8)
    assert JC.takes("loop.step_verify", "met", 0.97, 0.95, ["met"])
    assert not JC.takes("loop.step_verify", "met", 0.9, 0.95, ["met"])           # below tau
    assert not JC.takes("loop.step_verify", "not_met", 0.01, 0.95, ["met"])      # not an allowed label
    assert not JC.takes("loop.step_verify", None, None, 0.95, ["met"])


def test_student_settings_and_the_switch(monkeypatch):
    f = SB.REGISTRY["decide.student.step_verify"]
    assert f.modes[:3] == ("off", "shadow", "first") and f.default == "off"
    assert ST.mode("loop.step_verify") == "off" and ST.mode("loop.tier") == "off"
    eff = ST.SB.resolve(call={"decide.student.step_verify": "shadow"})
    token = ST.SB.RUN_MINDS.set(eff)                  # the switchboard module the student reads
    try:
        assert ST.mode("loop.step_verify") == "shadow"
    finally:
        ST.SB.RUN_MINDS.reset(token)
    monkeypatch.setenv("VERA_DECIDE_STUDENT_TAU", "0.9")
    monkeypatch.setenv("VERA_DECIDE_STUDENT_VERIFY_LABELS", "met,not_met")
    assert ST.tau() == 0.9 and ST.allowed("loop.step_verify") == ["met", "not_met"]
    assert ST.decides("loop.step_verify", {"answer": "not_met", "p": 0.05})
    monkeypatch.setenv("VERA_DECIDE_STUDENT_TAU", "nonsense")
    assert ST.tau() == 0.95 and not ST.decides("loop.step_verify", None)


def test_every_verdict_says_who_decided():
    assert LD.verify_by("llm", "qwen3.5:35b") == ("llm", "qwen3.5:35b", "llm_only")
    assert LD.verify_by("rule:cited_path_missing") == ("rule", "rule:cited_path_missing", "rule")
    assert LD.verify_by("rule") == ("rule", "rule", "rule")
    assert LD.verify_by("fallback") == ("rule", "fallback:ok_flag", "rule")
    stu = {"answer": "met", "p": 0.98, "impl": "llm:qwen3:4b-instruct-2507-q4_K_M"}
    assert LD.verify_by("student", student=stu) == ("student", stu["impl"], "student_first")
    d = LD.verify_decision("s", "s1", True, 1, model="big", by="student", student=stu)
    assert d["decider"] == "student" and d["mode"] == "student_first" and d["p"] == 0.98
    assert d["detail"]["by"] == "student"
    r = LD.verify_decision("s", "s1", False, 1, model="big", by="rule")
    assert r["decider"] == "rule" and r["p"] is None


def test_a_student_that_did_not_decide_is_recorded_as_a_shadow():
    rows = []
    rl = loop_ledger.RunLedger("sess", origin="census", sink=rows.append)
    stu = {"answer": "not_met", "p": 0.03, "impl": "llm:qwen3:4b", "mass": 0.99, "ms": 812}
    rl.verify("s1", True, 1, model="big", inputs={"evidence": "E"}, by="llm", student=stu)
    assert [r["mode"] for r in rows] == ["llm_only", "shadow"]
    live, shadow = rows
    assert shadow["detail"]["for"] == live["decision_id"] and shadow["answer"] == "not_met"
    assert shadow["implementation"] == "llm:qwen3:4b" and shadow["ms"] == 812
    assert rl.states == [(live["decision_id"], {"evidence": "E"})]          # the shadow keeps no inputs
    rows.clear()
    rl.verify("s2", True, 1, model="big", by="student", student=dict(stu, answer="met", p=0.99))
    assert [r["mode"] for r in rows] == ["student_first"]                   # it decided: no shadow of itself
    rows.clear()
    rl.verify("s3", False, 1, model="big", by="rule:x")
    assert [r["decider"] for r in rows] == ["rule"]


def test_the_whole_evidence_is_kept_for_the_student():
    ev = "E" * 15000
    out = TS.render({"evidence": ev, "evidence_system": "R" * 5000, "result": "r" * 9000})
    assert out["evidence"] == ev and out["evidence_system"] == "R" * 5000
    assert len(json.dumps(out)) <= TS.MAX_TOTAL + 100
    assert len(TS.render({"result": "r" * 9000})["result"]) <= TS.MAX_FIELD + 1


def test_the_verifier_asks_the_student_before_the_big_model_and_tells_the_ledger():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    body = src[src.index("async def _v6_verify_step"):src.index("_V6_FINALIZE_MIN_RAW =")]
    assert body.index('_StudentHook.start("loop.step_verify", sys, prompt)') < body.index(
        "await _safe_ollama_generate_dw(")                                  # the same evidence, the live rules
    assert '"by": "student"' in body and '"by": "llm"' in body and '"by": "fallback"' in body
    assert body.index("_settled = _verify_evidence.settle_test_criterion") < body.index("_StudentHook.start(")
    assert 'by=v.get("by") or "rule", student=v.get("student"))' in src
    assert '"evidence": v.get("evidence"), "evidence_system": v.get("evidence_system")' in src
    judge = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert "system=JC.system_for(question_id, state)" in judge
    assert "COALESCE(l.row->>'decider', 'llm') <> 'rule'" in judge


CTRL_SYS = ("You steer an agentic run after each step.\nAVAILABLE CAPABILITIES (name - description):\n- a.b - x\n"
            'Respond ONLY with JSON:\n{"findings":"...","action":"continue|insert|replan|stop"}')


def test_the_controller_student_gets_the_live_ledger_and_rules_in_one_letter():
    s = JC.student_system(CTRL_SYS, "loop.controller_action")
    assert "AVAILABLE CAPABILITIES" in s and "JSON" not in s and s.endswith(JC.STUDENT_CONTROLLER_ANSWER)
    state = {"evidence": "LEDGER: ...", "evidence_system": CTRL_SYS, "goal": "g"}
    assert JC.user_message("loop.controller_action", state) == "LEDGER: ..."
    assert JC.system_for("loop.controller_action", state) == s
    assert "Answer with one letter." in JC.user_message("loop.controller_action", {"goal": "g"})
    assert JC.student_system(LIVE_SYS) == JC.student_system(LIVE_SYS, "loop.step_verify")


def test_the_controller_switch_and_its_allowed_moves(monkeypatch):
    f = SB.REGISTRY["decide.student.controller_action"]
    assert f.modes == ("off", "shadow", "first") and f.default == "off" and "steer" in f.description
    assert ST.FEATURE["loop.controller_action"] == "decide.student.controller_action"
    assert ST.allowed("loop.controller_action") == ["continue"]
    assert ST.decides("loop.controller_action", {"answer": "continue", "p": 0.97})
    assert not ST.decides("loop.controller_action", {"answer": "stop", "p": 0.99})
    monkeypatch.setenv("VERA_DECIDE_STUDENT_CONTROLLER_LABELS", "continue,stop")
    assert ST.decides("loop.controller_action", {"answer": "stop", "p": 0.99})


def test_every_controller_move_says_who_decided_and_keeps_the_student():
    stu = {"answer": "continue", "p": 0.98, "impl": "llm:qwen3:4b"}
    d = LD.controller_decision("s", 3, "continue", {}, model="big", by="student", student=stu)
    assert (d["decider"], d["mode"], d["p"], d["detail"]["by"]) == ("student", "student_first", 0.98, "student")
    f = LD.controller_decision("s", 3, "continue", {}, model="big", by="fallback")
    assert (f["decider"], f["implementation"]) == ("rule", "fallback:ok_flag")
    assert LD.controller_decision("s", 3, "stop", {}, model="big")["decider"] == "llm"
    rows = []
    rl = loop_ledger.RunLedger("sess", origin="census", sink=rows.append)
    rl.controller(3, "replan", {"steps": [{}]}, model="big", inputs={"evidence": "E"}, by="llm",
                  student=dict(stu, answer="continue", p=0.6))
    assert [r["mode"] for r in rows] == ["llm_only", "shadow"] and rows[1]["detail"]["for"] == rows[0]["decision_id"]
    rows.clear()
    rl.controller(4, "continue", {}, model="big", by="student", student=stu)
    assert [r["mode"] for r in rows] == ["student_first"]


def test_the_controller_asks_the_student_before_the_big_model_and_a_student_writes_no_steps():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "class _StudentHook:" in src
    body = src[src.index("async def _v6_control("):src.index("async def _v6_final_gate(")]
    assert body.index('_StudentHook.start("loop.controller_action", sys, prompt)') < body.index(
        "await _safe_ollama_generate_dw(")
    assert '_stu_hook.decided["answer"] in ("continue", "stop")' in body
    assert body.count('"by": "fallback"') == 2 and '"by": "llm", "student": await _stu_hook.result()' in body
    assert 'by=ctrl.get("by") or "llm", student=ctrl.get("student"))' in src
    assert '"evidence": ctrl.get("evidence"), "evidence_system": ctrl.get("evidence_system")' in src


def test_the_student_and_its_judge_can_run_on_a_named_node(monkeypatch):
    monkeypatch.delenv("VERA_DECIDE_STUDENT_INSTANCE", raising=False)
    assert ST.instance() == ""
    monkeypatch.setenv("VERA_DECIDE_STUDENT_INSTANCE", "cpu-247")
    assert ST.instance() == "cpu-247"
    src = open(os.path.join(ROOT, "vera", "minds", "student.py"), encoding="utf-8").read()
    assert "instance_id=node or None, prefer_gpu=not node" in src and 'f"@{node}" if node else ""' in src
    judge = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert 'impl = _impl(model) + (f"@{instance}" if instance else "")' in judge
    assert "instance_id=instance or None, prefer_gpu=not instance" in judge
    assert JC.latency([900, 100, 300, 0]) == {"ms_p50": 300, "ms_p90": 900} and JC.latency([]) == {}


def test_each_verify_label_is_its_own_mode_for_a_census_arm(monkeypatch):
    f = SB.REGISTRY["decide.student.step_verify"]
    assert f.modes == ("off", "shadow", "first", "first_met", "first_not_met") and f.default == "off"
    assert SB.validate({"decide.student.step_verify": "first_not_met"}, census=True)["ok"]
    monkeypatch.setenv("VERA_DECIDE_STUDENT_VERIFY_LABELS", "met")
    assert ST.allowed("loop.step_verify", "first_not_met") == ["not_met"]
    assert ST.allowed("loop.step_verify", "first_met") == ["met"] and ST.allowed("loop.step_verify", "first") == ["met"]
    assert ST.decides("loop.step_verify", {"answer": "not_met", "p": 0.01}, "first_not_met")
    assert not ST.decides("loop.step_verify", {"answer": "met", "p": 0.99}, "first_not_met")
    assert all(ST.is_first(m) for m in ("first", "first_met", "first_not_met")) and not ST.is_first("shadow")


def test_the_cosign_sample_and_its_mark(monkeypatch):
    monkeypatch.setenv("VERA_DECIDE_STUDENT_COSIGN", "0.1")
    assert ST.cosigned(lambda: 0.05) and not ST.cosigned(lambda: 0.5)
    monkeypatch.setenv("VERA_DECIDE_STUDENT_COSIGN", "x")
    assert ST.cosign_rate() == 0.1
    rows = []
    rl = loop_ledger.RunLedger("sess", origin="census", sink=rows.append)
    rl.verify("s1", True, 1, model="big", by="llm",
              student={"answer": "met", "p": 0.99, "impl": "llm:qwen3:4b", "would_take": True})
    assert rows[1]["mode"] == "shadow" and rows[1]["detail"]["would_take"] is True
    rows.clear()
    rl.verify("s2", True, 1, model="big", by="llm", student={"answer": "not_met", "p": 0.2, "impl": "x"})
    assert "would_take" not in rows[1]["detail"]
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    hook = src[src.index("class _StudentHook:"):src.index("async def result(self)")]
    assert "_student.decides(question_id, h._answer, mode)" in hook and "_student.cosigned()" in hook
    assert "would_take=True" in hook


def test_the_student_is_the_small_model_that_fits_beside_the_9b():
    from vera.minds import student as S
    assert S.DEFAULT_MODEL == "qwen3:1.7b"
