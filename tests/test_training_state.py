"""Decisions keep their inputs for training (four-minds FM-0.2, the payload rule)."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC, loop_ledger, training_state_core as TS  # noqa: E402

pytestmark = pytest.mark.critical


def test_surfaces_default_to_loop_discovery_and_intel():
    # discovery joined 2026-10-10: the drift guard's page verdicts (public web excerpts) are labelled for the
    # FM-7.5 200-page set; intel joined 2026-10-11 (FM-13.6: the gates' signals, rated in /decide/labels);
    # chat and memory stay off until asked for
    want = {"loop", "discovery", "intel"}
    assert TS.enabled_surfaces(None) == want and TS.enabled_surfaces("") == want
    assert TS.enabled_surfaces("none") == set()
    assert TS.enabled_surfaces("loop, chat") == {"loop", "chat"}


def test_render_redacts_and_bounds():
    cred = lambda s: s.replace("hunter2", "<redacted>")
    out = TS.render({"goal": "email boe@example.com or ring 07700 900123, pw hunter2, on 2026-10-08 v1.2.3",
                     "result": "x" * 10000, "done": list(range(100))}, cred)
    g = out["goal"]
    assert "<email>" in g and "<phone>" in g and "<redacted>" in g
    assert "2026-10-08" in g and "1.2.3" in g                  # dates and versions are not phone numbers
    assert len(out["result"]) <= TS.MAX_FIELD + 1 and len(out["done"]) == TS.MAX_LIST
    huge = TS.render({f"k{i}": "y" * 4000 for i in range(30)})
    assert len(json.dumps(huge)) <= TS.MAX_TOTAL


def test_only_enabled_surfaces_are_wanted():
    d = {"decision_id": "dec_" + "0" * 32, "surface": "loop"}
    assert TS.wanted(d, {"loop"}) and not TS.wanted(d, {"chat"}) and not TS.wanted({}, {"loop"})


def test_the_run_ledger_keeps_each_decisions_inputs():
    rows = []
    rl = loop_ledger.RunLedger("sess-1", origin="census", sink=rows.append)
    rl.intent("build", {"heuristic": "build", "llm": ""}, inputs={"goal": "write a lan scanner"})
    rl.tier("simple", {"heuristic": "simple"}, inputs={"goal": "write a lan scanner"})
    rl.verify(1, True, 1, inputs={"step": "scan", "success": "hosts listed", "result": "3 hosts"})
    rl.controller(1, "continue", {}, inputs={"goal": "g", "done": [{"title": "scan", "ok": True}]})
    rl.verify(2, None, 1, inputs={"step": "x"})                  # verification off: no row, nothing kept
    rl.step_done(1, "", 1, inputs={"step": "x"})                 # no rule fired: no row, nothing kept
    rl.prestep(2, [], 1)                                         # no inputs given: the row only
    assert len(rows) == 5
    kept = dict(rl.states)
    assert set(kept) == {r["decision_id"] for r in rows[:4]}
    assert kept[rows[0]["decision_id"]] == {"goal": "write a lan scanner"}
    assert all(LC.check_decision(r) == [] for r in rows)          # the row itself still holds no payload


def test_every_loop_decision_site_passes_its_inputs():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    for call in ('_dl.tier(tier, tier_info, model=str(model or ""), inputs={"goal": goal},',
                 '_dl.intent(intent, _intent_info, model=str(model or ""), inputs={"goal": goal})'):
        assert call in src, call
    for head in ('_dl.verify(step.get("id"), v["met"], len(_vr), model=str(model or ""),\n',
                 '_dl.controller(step["id"], action, ctrl, model=str(model or ""),\n',
                 '_dl.prestep(step["id"], pinfo.get("gaps") or [], len(results), model=str(model or ""),\n'):
        i = src.index(head)
        assert "inputs={" in src[i:i + 400], head
    i = src.index('_dl.step_done(step.get("id"), res["done_by"],')
    assert "inputs={" in src[i:i + 300]


def test_the_store_is_registered():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/training_state.py"),' in src


def test_the_zero_shot_intent_is_a_shadow_of_the_live_one():
    from vera.minds import loop_decisions_core as LD
    rows = []
    rl = loop_ledger.RunLedger("sess-2", origin="user", sink=rows.append)
    rl.intent("build", {"heuristic": "build", "llm": ""}, inputs={"goal": "make a timer app"})
    rec = {"zeroshot": "build", "scores": {"build": 0.91, "research": 0.2, "action": 0.05}, "margin": 0.71,
           "confidence": 0.91, "used": "build", "heuristic": "build", "llm": "", "agrees_used": True,
           "agrees_llm": None}
    rl.intent_shadow(rec, inputs={"goal": "make a timer app"})
    rl.intent_shadow({"zeroshot": None}, inputs={"goal": "x"})          # no answer: nothing
    live, shadow = rows
    assert LC.check_decision(shadow) == []
    assert shadow["question_id"] == live["question_id"] == LD.INTENT_Q
    assert shadow["mode"] == "shadow" and shadow["implementation"] == "nlp.zeroshot" and shadow["p"] == 0.91
    assert shadow["state_hash"] == live["state_hash"] and shadow["state_ref"] == live["state_ref"]
    assert shadow["detail"]["agrees_used"] is True
    assert len(rl.states) == 2
    rl.finish({"complete": True, "missing": []})
    assert not [r for r in rows if r.get("schema") == LC.OUTCOME_SCHEMA
                and r["decision_id"] == shadow["decision_id"]]                # a shadow is never judged here


def test_the_runner_records_the_intent_shadow():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "_zs_rec = await _v6_intent_measure(_intent_zs_task" in src
    assert '_dl.intent_shadow(_zs_rec, inputs={"goal": goal})' in src


def test_goal_decisions_keep_the_goal_frame(monkeypatch):
    TSR = pytest.importorskip("Vera.vera.minds.training_state", reason="needs the app")
    import asyncio as _aio
    seen = []

    async def fake_frame(text="", kind="", **kw):
        return {"domain": "build", "domains": [{"domain": "build", "score": 0.6}], "patterns": {"paths": []},
                "degraded": False, "cards": "v1", "vector": [0.1]}
    reg = {"sense.frame": {"raw": fake_frame}}
    import Vera.vera.capability_orchestration as orch
    monkeypatch.setattr(orch, "CAPABILITY_REGISTRY", reg)
    monkeypatch.setattr(TSR, "put", lambda d, i: seen.append((d["decision_id"], i)))
    monkeypatch.setattr(TSR, "surfaces", lambda: {"loop"})
    d = {"decision_id": "dec_" + "1" * 32, "surface": "loop", "question_id": "loop.intent"}

    async def go():
        TSR.put_framed(d, {"goal": "write a timer app"})
        for _ in range(5):
            await _aio.sleep(0)
    _aio.run(go())
    (did, inputs), = seen
    assert inputs["goal"] == "write a timer app" and inputs["frame"]["domain"] == "build"
    assert "vector" not in inputs["frame"]                      # the 384-d vector is not stored


def test_the_ledger_routes_goal_questions_through_the_frame():
    src = open(os.path.join(ROOT, "vera", "minds", "loop_ledger.py"), encoding="utf-8").read()
    assert 'TSR.put_framed(row, inputs)' in src
    ts = open(os.path.join(ROOT, "vera", "minds", "training_state.py"), encoding="utf-8").read()
    assert 'FRAME_QUESTIONS = ("loop.intent", "loop.tier")' in ts
