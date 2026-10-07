"""A delegated job becomes a trajectory: why, what, how, outcome - and a rating."""

import pytest

from vera.evolve import delegate_trajectory_core as T

JOB = {"id": "dg123", "session_id": "delegate:dg123", "title": "where is X built",
       "parent_task": "Roadmap G: intent-aware catalogue", "delegator": "claude:abc",
       "brief": "Find where the catalogue is built.", "plan": ["grep", "read"],
       "suggest_caps": ["evolve.delegate.fs.grep"], "goal": "DELEGATED TASK ...",
       "mode": "report", "effort": "standard", "ref": "bleeding-edge", "head": "abc1234",
       "plan_style": "stepwise", "status": "done", "created_at": "t0", "ended_at": "t1",
       "report": "## Summary\nIt is built in dag.py.\n\n## Findings\n- `a.py:10` builds it\n\n## Open questions\n- none"}

EVENTS = [
    {"type": "agent_loop_v6.toolkit", "toolkit": ["evolve.delegate.fs.grep", "evolve.delegate.fs.read"]},
    {"type": "agent_loop_v6.tier", "tier": "complex"},
    {"type": "agent_loop_v6.intent", "intent": "research"},
    {"type": "agent_loop_v6.intent_core", "front": ["evolve.delegate.fs.grep"]},
    {"type": "agent_loop_v6.plan", "steps": [{"title": "find it", "caps": ["evolve.delegate.fs.grep"]}]},
    {"type": "agent_loop_v5.step_start", "step_id": 1, "title": "find it", "goal": "grep",
     "caps": ["evolve.delegate.fs.grep"]},
    {"type": "agent_loop_v5.tool_call", "step_id": 1, "cycle": 1, "tool": "evolve.delegate.fs.grep",
     "args": {"pattern": "catalog"}, "thought": "search for the builder"},
    {"type": "agent_loop_v5.tool_done", "step_id": 1, "cycle": 1, "tool": "evolve.delegate.fs.grep",
     "ok": True, "preview": "a.py:10", "elapsed_ms": 12},
    {"type": "agent_loop_v5.tool_done", "step_id": 1, "cycle": 2, "tool": "read", "ok": False,
     "error": "There is NO capability called 'read'"},
    {"type": "agent_loop_v5.step_done", "step_id": 1, "ok": True, "summary": "found it"},
    {"type": "agent_loop_v6.done", "reason": "complete"},
]


def test_build_carries_why_what_how_and_outcome():
    t = T.build(JOB, EVENTS, metrics={"findings": 1})
    assert t["parent_task"] == "Roadmap G: intent-aware catalogue"
    assert t["delegator"] == "claude:abc"
    assert t["brief"].startswith("Find where") and t["plan"] == ["grep", "read"]
    assert t["loop"]["tier"] == "complex" and t["loop"]["intent"] == "research"
    assert t["loop"]["intent_core_front"] == ["evolve.delegate.fs.grep"]
    assert t["loop"]["plan"][0]["title"] == "find it"
    assert t["loop"]["done_reason"] == "complete"
    [s] = t["steps"]
    assert s["title"] == "find it" and s["ok"] is True and s["summary"] == "found it"
    call, bad = s["calls"]
    assert call["tool"] == "evolve.delegate.fs.grep" and call["ok"] is True
    assert call["thought"] == "search for the builder" and call["args"] == {"pattern": "catalog"}
    assert call["result"] == "a.py:10"
    assert bad["tool"] == "read" and bad["ok"] is False and bad["args"] is None
    assert t["counts"] == {"steps": 1, "calls": 2, "failed_calls": 1, "unknown_tools": 1,
                           "truncated": False}
    assert t["metrics"] == {"findings": 1} and t["rating"] is None


def test_string_booleans_from_the_event_log_are_read():
    ev = [{"type": "agent_loop_v5.tool_call", "step_id": "1", "cycle": "1", "tool": "x"},
          {"type": "agent_loop_v5.tool_done", "step_id": "1", "cycle": "1", "tool": "x", "ok": "False"}]
    assert T.build({}, ev)["steps"][0]["calls"][0]["ok"] is False


def test_calls_are_bounded():
    ev = [{"type": "agent_loop_v5.tool_call", "step_id": 1, "cycle": i, "tool": "t"}
          for i in range(T.MAX_CALLS + 20)]
    t = T.build({}, ev)
    assert t["counts"]["calls"] == T.MAX_CALLS and t["counts"]["truncated"] is True


def test_large_args_are_truncated_to_a_string():
    ev = [{"type": "agent_loop_v5.tool_call", "step_id": 1, "cycle": 1, "tool": "t",
           "args": {"x": "y" * 5000}}]
    a = T.build({}, ev)["steps"][0]["calls"][0]["args"]
    assert isinstance(a, str) and len(a) == T.MAX_ARGS


def test_rate_sets_and_keeps_history():
    t = T.build(JOB, EVENTS)
    t1 = T.rate(t, "partly", "missed one file", by="claude", at="t2")
    assert t1["rating"]["verdict"] == "partly" and t["rating"] is None
    t2 = T.rate(t1, "USEFUL", by="claude")
    assert t2["rating"]["verdict"] == "useful" and t2["rating_history"][0]["verdict"] == "partly"
    with pytest.raises(ValueError):
        T.rate(t, "great")


def test_only_rated_jobs_are_remembered():
    t = T.build(JOB, EVENTS)
    assert T.memory_text(t) == ""
    m = T.memory_text(T.rate(t, "useful", "exact refs", by="claude"))
    assert "Part of: Roadmap G" in m and "Asked: Find where" in m
    assert "It is built in dag.py." in m and "`a.py:10`" in m and "rated useful" in m


def test_a_wrong_report_is_remembered_as_wrong_not_as_an_answer():
    m = T.memory_text(T.rate(T.build(JOB, EVENTS), "wrong", "cited files that do not exist"))
    assert "judged WRONG" in m and "It is built in dag.py." not in m


def test_tags_importance_and_row():
    r = T.rate(T.build(JOB, EVENTS), "useful")
    assert T.memory_tags(r) == "delegate,verdict:useful,intent:research,mode:report"
    assert T.memory_importance(r) > T.memory_importance(T.rate(r, "wrong"))
    row = T.summary_row(r)
    assert row["verdict"] == "useful" and row["calls"] == 2


def test_section():
    assert T.section(JOB["report"], "summary") == "It is built in dag.py."
    assert T.section(JOB["report"], "finding") == "- `a.py:10` builds it"
    assert T.section("no headings", "summary") == ""
