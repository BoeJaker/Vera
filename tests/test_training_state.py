"""Decisions keep their inputs for training (four-minds FM-0.2, the payload rule)."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC, loop_ledger, training_state_core as TS  # noqa: E402

pytestmark = pytest.mark.critical


def test_surfaces_default_to_loop_only():
    assert TS.enabled_surfaces(None) == {"loop"} and TS.enabled_surfaces("") == {"loop"}
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
    for call in ('_dl.tier(tier, tier_info, model=str(model or ""), inputs={"goal": goal})',
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
