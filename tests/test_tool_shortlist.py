"""caps.shortlist: tools from what similar past steps used (four-minds FM-3)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC, tool_shortlist_core as TC  # noqa: E402

pytestmark = pytest.mark.critical


def test_steps_from_a_trace_keep_only_tools_that_worked():
    tr = {"steps": [{"step_id": 1, "title": "Research X", "calls": [
        {"tool": "web.research", "ok": True}, {"tool": "web.fetch", "ok": False}, {"tool": "web.research", "ok": True}]},
        {"step_id": 2, "title": "", "calls": [{"tool": "code.author", "ok": True}]},
        {"step_id": 3, "title": "Write it", "calls": []}]}
    assert TC.steps_from_trace(tr) == [(1, "Research X", ["web.research"])]
    assert TC.steps_from_trace({}) == []


INDEX = [{"run": "r1", "vec": [1.0, 0.0], "tools": ["web.research"]},
         {"run": "r2", "vec": [0.9, 0.1], "tools": ["web.research", "prose.author"]},
         {"run": "r3", "vec": [0.0, 1.0], "tools": ["code.author"]},
         {"run": "r4", "vec": [0.1, 0.9], "tools": ["code.author", "exec.bash.run"]}]


def test_the_nearest_steps_vote_and_the_prior_fills_the_rest():
    out = TC.shortlist([1.0, 0.05], INDEX, k=4, neighbours=2)
    names = [x["name"] for x in out]
    assert names[0] == "web.research" and names[1] == "prose.author"
    assert {x["by"] for x in out[:2]} == {"neighbours"} and out[2]["by"] == "prior"
    assert set(names) == {"web.research", "prose.author", "code.author", "exec.bash.run"}


def test_a_run_never_votes_for_itself():
    names = [x["name"] for x in TC.shortlist([1.0, 0.0], INDEX, k=3, neighbours=1, exclude_run="r1")]
    assert names[0] in ("web.research", "prose.author")      # r2's vote, not r1's
    only_r1 = TC.shortlist([1.0, 0.0], INDEX[:1], exclude_run="r1")
    assert only_r1 == []


def test_no_vector_falls_back_to_the_prior():
    out = TC.shortlist([], INDEX, k=2)
    assert [x["by"] for x in out] == ["prior", "prior"] and out[0]["name"] in ("web.research", "code.author")


def test_decision_and_outcomes():
    d = TC.decision(["web.research", "prose.author", "code.author"], run_id="sess-1", step_id=2, origin="census")
    assert LC.check_decision(d) == [] and d["mode"] == "shadow" and d["question_id"] == TC.QUESTION
    outs = TC.outcomes(d, ["prose.author", "exec.bash.run"])
    got = sorted((o["label"], o["detail"].get("rank"), o["detail"]["tool"]) for o in outs)
    assert got == [("missed", None, "exec.bash.run"), ("used", 2, "prose.author")]
    assert all(o["strength"] == "strong" and o["source"] == "step.tools" for o in outs)


def test_summary():
    s = TC.summarize([([1], 1), ([7], 2), ([], 1)])
    assert s == {"n": 3, "hit@5": round(1 / 3, 3), "recall@10": round((1 + 0.5 + 0) / 3, 3), "mrr": round((1 + 1 / 7) / 3, 3)}
    assert TC.summarize([]) == {"n": 0}


def test_the_loop_records_each_finished_step():
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert src.count("_tool_shortlist_step(sid, step, res, _dl.origin)") == 2
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/tool_shortlist.py"),' in orch


def test_capabilities_are_called_by_keyword():
    """A @capability wrapper takes keyword arguments only: the positional call made every live shortlist record fail
    silently (2026-10-09)."""
    import re
    for rel in (("vera", "minds", "tool_shortlist.py"), ("vera", "minds", "judge.py")):
        src = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
        assert not re.findall(r"await cap_\w+\((?!\)|\w+=)", src), rel


def test_the_replay_holds_each_run_out_and_counts_what_no_past_run_used():
    # FM-3.2: each step asks the shortlist with its own run held out; prose.author (r2 only) and exec.bash.run (r4
    # only) were never used by another run, so no shortlist built from the past can offer them
    out = TC.replay(INDEX)
    assert out["n"] == 4 and out["tools_used"] == 6
    assert out["unknown_rate"] == round(2 / 6, 3)
    assert out["recall@28"] == round(4 / 6, 3) and out["hit@5"] == 1.0
    assert out["recall@5"] <= out["recall@10"] <= out["recall@28"] and 0 < out["mrr"] <= 1.0
    assert out["median_position"] in (1, 2)
    assert TC.replay([]) == {"n": 0} and TC.replay([{"run": "x", "vec": [], "tools": ["a"]}]) == {"n": 0}


def test_the_night_job_replays_after_a_successful_build_and_keeps_snapshots():
    src = open(os.path.join(ROOT, "vera", "minds", "tool_shortlist.py"), encoding="utf-8").read()
    job = src[src.index("async def _night_job("):src.index("# On the Loop Lab schedule")]
    assert job.index('await r.set(KEY_NIGHT + night, "1", ex=86400)') < job.index("await run_replay(night)")
    rep = src[src.index("async def run_replay("):src.index("try:                                     # pragma: no cover - import-shape only\n    from Vera.vera.minds import chat_tools_core")]
    assert "asyncio.to_thread(TC.replay, rows)" in rep and "ltrim(KEY_REPLAY, 0, REPLAY_KEEP - 1)" in rep
    assert '"caps.shortlist.replay"' in src
