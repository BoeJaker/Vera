"""The loop's controller and verifier decisions reach the decision ledger (FM-0.2a)."""
import ast
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC, loop_decisions_core as LD, loop_ledger  # noqa: E402

pytestmark = pytest.mark.critical


def _run(sink):
    return loop_ledger.RunLedger("sess-1", origin="census", digest=lambda: "abc123def456", sink=sink)


def test_rows_are_valid_v0_and_carry_the_run():
    rows = []
    rl = _run(rows.append)
    rl.verify(1, False, 1, model="qwen3.5:9b")
    rl.verify(1, True, 2, model="qwen3.5:9b")
    rl.controller(1, "continue", {"goal_met": False, "goal_alignment": "on track", "steps": []}, model="qwen3.5:9b")
    rl.verify(2, None, 1)                       # verification off: no row
    assert len(rows) == 3
    for r in rows:
        assert LC.check_decision(r) == [], LC.check_decision(r)
        assert r["origin"] == "census" and r["minds_digest"] == "abc123def456" and r["run_id"] == "sess-1"
    assert [r["question_id"] for r in rows] == [LD.VERIFY_Q, LD.VERIFY_Q, LD.CONTROLLER_Q]


def test_outcomes_retry_continue_and_stop():
    rows = []
    rl = _run(rows.append)
    rl.verify(1, False, 1)                     # not_met, retried and then met -> correct
    rl.verify(1, True, 2)
    rl.controller(1, "continue", {})           # next verified step (2) met -> correct
    rl.verify(2, True, 1)
    rl.controller(2, "continue", {})           # next verified step (3) not met -> wrong
    rl.verify(3, False, 1)
    rl.controller(3, "stop", {})               # gate names a gap -> wrong
    n = rl.finish({"complete": False, "missing": ["the summary file"]})
    outs = [r for r in rows if r["schema"] == LC.OUTCOME_SCHEMA]
    assert n == len(outs) == 4
    for o in outs:
        assert LC.check_outcome(o) == []
    by_src = sorted((o["source"], o["label"]) for o in outs)
    assert by_src == [("run.gate_missing", "wrong"), ("run.next_step_verify", "correct"),
                      ("run.next_step_verify", "wrong"), ("run.retry_passed", "correct")]


def test_stop_without_a_gate_has_no_outcome():
    rows = []
    rl = _run(rows.append)
    rl.controller(1, "stop", {})
    assert rl.finish({}) == 0


def test_origin_mapping():
    assert LD.origin_for("census") == "census" and LD.origin_for("claude") == "delegate"
    assert LD.origin_for("") == "user" and LD.origin_for(None) == "user"


def test_switch_off_sends_nothing(monkeypatch):
    monkeypatch.setenv("VERA_DECIDE_LEDGER_LOOP", "0")
    rows = []
    rl = _run(rows.append)
    rl.controller(1, "continue", {})
    assert rows == [] and rl.decisions          # still collected, not sent


def test_background_send_never_blocks_or_raises(monkeypatch):
    calls = []

    async def fake_append(row):
        calls.append(row["decision_id"])
        return {"ok": True}
    monkeypatch.setattr(loop_ledger, "_append", lambda: fake_append)

    async def go():
        rl = loop_ledger.RunLedger("s", digest=lambda: 1 / 0)    # a broken digest falls back
        rl.controller(1, "continue", {})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return rl
    rl = asyncio.run(go())
    assert len(calls) == 1 and rl.decisions[0]["minds_digest"] == "default"


def test_runner_hooks_are_wired():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "_dl.controller(step[\"id\"], action, ctrl" in src
    assert "_dl.verify(step.get(\"id\"), v[\"met\"], len(_vr)" in src
    assert "_dl.finish(_gate_last, steps_run=len(results)" in src
    assert "_dl.tier(tier, tier_info" in src and "_dl.intent(intent, _intent_info" in src
    assert "_dl.finish({}, steps_run=1, fast_path=True)" in src
    for line in src.splitlines():
        if "_dl." in line and ("await " in line):
            pytest.fail("decision recording must not add an await: " + line.strip())


def test_declared_origin_wins_when_it_is_a_v0_origin():
    assert LD.origin_for("claude", "census") == "census"
    assert LD.origin_for("claude", "nonsense") == "delegate"
    assert LD.origin_for("", "") == "user"


def test_census_harness_declares_its_origin():
    src = open(os.path.join(ROOT, "tools", "loop-census", "run_census.py"), encoding="utf-8").read()
    assert '_args["decision_origin"] = "census"' in src



def test_tier_and_intent_rows_and_the_tier_outcome():
    rows = []
    rl = _run(rows.append)
    rl.tier("complex", {"heuristic": "simple", "llm": "complex", "suggested": "complex"}, model="q")
    rl.intent("build", {"heuristic": "build", "llm": ""})
    assert [r["question_id"] for r in rows] == ["loop.tier", "loop.intent"]
    assert rows[0]["decider"] == "llm" and rows[1]["decider"] == "rule"
    assert all(LC.check_decision(r) == [] for r in rows)
    rl.finish({"complete": True, "missing": []}, steps_run=5)
    outs = [r for r in rows if r["schema"] == LC.OUTCOME_SCHEMA]
    assert [(o["source"], o["label"]) for o in outs] == [("run.tier_band", "correct")]


def test_tier_outcome_rules():
    def d(t):
        return LD.tier_decision("s", t, {})
    assert LD.tier_outcome(d("single"), 1, True, {})["label"] == "correct"
    assert LD.tier_outcome(d("single"), 6, False, {})["label"] == "wrong"
    assert LD.tier_outcome(d("single"), 6, None, {}) is None
    assert LD.tier_outcome(d("simple"), 7, None, {})["label"] == "wrong"
    assert LD.tier_outcome(d("complex"), 4, None, {"missing": ["the report"]})["label"] == "wrong"
    assert LD.tier_outcome(d("strategic"), 40, None, {})["label"] == "correct"
    assert LD.tier_outcome(d("complex"), None, None, {}) is None
