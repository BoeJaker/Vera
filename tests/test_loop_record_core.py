"""Every finished agent loop becomes one Loop Lab run record (loop_record_core).

A loop started from the chat, the dream director, a v8 program or the API used
to leave no run record at all - only census goals and Loop Lab tasks reached
`vera:evolve:runs`. These pin the record's shape and its ownership rules: one
loop is one row, and a census/task record of the same session always wins.
"""

from vera.dag import loop_trace_core as ltc
from vera.evolve import loop_record_core as lrc


def _events(sid="chat-abc", terminal="agent_loop_v6.done", **term):
    ev = [
        {"type": "agent_loop_v7.triage_start", "session_id": sid, "ts": "2026-09-27T10:00:00+00:00"},
        {"type": "agent_loop_v6.tier", "tier": "standard"},
        {"type": "agent_loop_v6.intent", "intent": "research"},
        {"type": "agent_loop_v6.plan_style", "effective": "broad", "requested": "broad", "reason": "asked"},
        {"type": "agent_loop_v6.plan", "steps": [{"id": 1, "title": "look"}, {"id": 2, "title": "write"}]},
        {"type": "agent_loop_v6.step_start", "step_id": 1, "title": "look"},
        {"type": "agent_loop_v6.tool_call", "step_id": 1, "tool": "web.research"},
        {"type": "agent_loop_v6.tool_done", "step_id": 1, "tool": "web.research", "ok": True},
        {"type": "agent_loop_v6.step_done", "step_id": 1, "ok": True},
    ]
    t = {"type": terminal, "session_id": sid, "ts": "2026-09-27T10:02:30+00:00"}
    t.update(term)
    return ev + [t]


def _build(sid="chat-abc", events=None, **kw):
    events = events if events is not None else _events(sid)
    return lrc.run_record_from_events(sid, events, ltc.digest_events(events), **kw)


def test_a_finished_chat_loop_becomes_a_record_with_the_digest_facts():
    compact, detail = _build(run_state={"goal": "compare two databases",
                                        "started_at": "2026-09-27T10:00:00+00:00"},
                             where="prod", ingested_at="now")
    assert compact["run_id"] == "chat-abc" and compact["loop_session"] == "chat-abc"
    assert compact["source"] == "loop" and compact["task"] == "loop:interactive"
    assert compact["status"] == "done" and compact["ok"] and compact["pass_rate"] == 1.0
    assert compact["elapsed_s"] == 150.0
    assert (compact["planned"], compact["executed"], compact["tool_calls"]) == (2, 1, 1)
    assert compact["plan_style"] == "broad" and compact["tier"] == "standard"
    assert compact["intent"] == "research" and compact["engine"] == "v7"
    assert compact["where"] == "prod" and compact["label"] == "compare two databases"
    assert detail["goal"] == "compare two databases" and detail["steps"]


def test_an_errored_loop_is_recorded_as_an_error():
    ev = _events(terminal="agent_loop_v6.error", error="planner timed out")
    compact, _ = _build(events=ev)
    assert compact["status"] == "error" and not compact["ok"]
    assert compact["pass_rate"] == 0.0 and compact["error"] == "planner timed out"


def test_origin_comes_from_the_session_prefix():
    assert lrc.origin_of("dream:c1:agent_loop") == "dream"
    assert lrc.origin_of("v8:p1:build:3") == "program"
    assert lrc.origin_of("census-run52-g1") == "census"
    assert lrc.origin_of("5f2c9a") == "interactive"


def test_loop_lab_task_sessions_are_left_to_the_task_runner():
    assert lrc.is_self_recording("evolve:abc123")
    assert _build(sid="evolve:abc123", events=_events("evolve:abc123")) is None


def test_an_unfinished_loop_is_not_recorded():
    assert _build(events=_events()[:-1]) is None


def test_a_loop_record_never_replaces_a_census_or_task_record():
    assert lrc.may_replace(None)
    assert lrc.may_replace({"source": "loop"})
    assert not lrc.may_replace({"source": "census"})
    assert not lrc.may_replace({"source": "manual"})
