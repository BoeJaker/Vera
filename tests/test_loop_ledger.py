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
    assert '_dl.step_done(step.get("id"), res["done_by"]' in src
    assert '_dl.prestep(step["id"], pinfo.get("gaps") or [], len(results)' in src
    assert '"actual_caps": _v5_actual_caps_used(history), "done_by": done_by}' in src
    for rule in ("research_done", "reauthor_done", "duplicate_call", "browser_done", "author_answered",
                 "same_tool_cap"):
        assert 'done_by = "%s"' % rule in src, rule
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


def test_step_done_rows_and_outcome():
    rows = []
    rl = _run(rows.append)
    rl.step_done(1, "", 1)                     # no rule ended it: no row
    rl.step_done(1, "duplicate_call", 1)
    rl.verify(1, True, 1)
    rl.step_done(2, "research_done", 1)
    rl.verify(2, False, 1)
    rl.step_done(3, "browser_done", 1)         # verification off for step 3: no outcome
    rl.verify(3, None, 1)
    ds = [r for r in rows if r["schema"] != LC.OUTCOME_SCHEMA]
    assert [d["question_id"] for d in ds].count(LD.STEP_DONE_Q) == 3
    assert all(LC.check_decision(d) == [] for d in ds)
    sd = [d for d in ds if d["question_id"] == LD.STEP_DONE_Q]
    assert sd[0]["decider"] == "rule" and sd[0]["implementation"] == "duplicate_call"
    rl.finish({})
    outs = [(o["source"], o["label"], o["detail"].get("rule")) for o in rows if o["schema"] == LC.OUTCOME_SCHEMA]
    assert outs == [("run.step_verify", "correct", "duplicate_call"), ("run.step_verify", "wrong", "research_done")]


def test_prestep_rows_and_one_sided_outcome():
    rows = []
    rl = _run(rows.append)
    rl.prestep(1, [], 0, model="q")            # no gaps, first attempt met -> correct
    rl.verify(1, True, 1)
    rl.prestep(2, [" "], 1)                    # blank gaps count as none; first attempt not met -> no outcome
    rl.verify(2, False, 1)
    rl.verify(2, True, 2)
    rl.prestep(3, ["the exchange rate"], 2)    # needed: no outcome
    rl.verify(3, True, 1)
    ds = [r for r in rows if r.get("question_id") == LD.PRESTEP_Q]
    assert [d["answer"] for d in ds] == ["not_needed", "not_needed", "needed"]
    assert all(LC.check_decision(d) == [] for d in ds)
    rl.finish({})
    outs = [o for o in rows if o["schema"] == LC.OUTCOME_SCHEMA and o["source"] == "run.first_attempt_met"]
    assert [o["decision_id"] for o in outs] == [ds[0]["decision_id"]]


def test_intent_outcome_by_the_kind_of_work_done():
    def d(i, mode="llm_only"):
        x = LD.intent_decision("s", i, {})
        x["mode"] = mode
        return x
    ok, miss = {"complete": True, "missing": []}, {"complete": False, "missing": ["the report"]}
    fam = lambda r, b: {"research": r, "build": b}
    assert LD.intent_outcome(d("build"), fam(2, 0), ok)["label"] == "wrong"
    assert LD.intent_outcome(d("build"), fam(0, 2), ok)["label"] == "correct"
    assert LD.intent_outcome(d("build"), fam(0, 2), miss) is None          # quality unknown: no verdict
    assert LD.intent_outcome(d("build"), fam(1, 1), ok) is None            # mixed evidence
    assert LD.intent_outcome(d("research"), fam(0, 1), ok)["label"] == "wrong"
    assert LD.intent_outcome(d("research"), fam(3, 1), ok)["label"] == "correct"
    assert LD.intent_outcome(d("mixed"), fam(2, 0), ok)["label"] == "wrong"
    assert LD.intent_outcome(d("mixed"), fam(1, 1), ok)["label"] == "correct"
    assert LD.intent_outcome(d("action"), fam(0, 1), ok) is None
    assert LD.intent_outcome(d("build", "shadow"), fam(2, 0), ok) is None  # a shadow is not judged here
    assert LD.intent_outcome(d("build"), fam(0, 0), ok) is None
    assert LD.intent_outcome(d("build"), None, ok) is None
    o = LD.intent_outcome(d("build"), fam(0, 2), ok)
    assert o["strength"] == "weak" and o["source"] == "run.cap_families"


def test_the_run_end_passes_the_kinds_of_work():
    rows = []
    rl = _run(rows.append)
    rl.intent("research", {"heuristic": "research", "llm": ""})
    rl.finish({"complete": True, "missing": []}, steps_run=2, families={"research": 2, "build": 0})
    outs = [r for r in rows if r["schema"] == LC.OUTCOME_SCHEMA]
    assert [(o["source"], o["label"]) for o in outs] == [("run.cap_families", "correct")]
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert "fast_path=(False if (enable_fast_path and tier == \"single\") else None), families=_fam)" in src
    assert "& _V5_RESEARCH_CAPS)" in src and "& _V5_AUTHORING_CAPS)}" in src
