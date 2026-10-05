"""How recalled memory is presented to a model (vera/fabric/recall_core.py).

Guards two defects measured on prod on 2026-10-04: capability calls and dream reports were injected as "User:"
lines, and one search filled four of twelve slots with two repeated sentences.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.fabric import recall_core as rc  # noqa: E402


# ── role_label ────────────────────────────────────────────────────────────────
def test_chat_turns_keep_their_two_roles():
    assert rc.role_label({"record_type": "message", "source_type": "human", "human_text": True}) == "User"
    assert rc.role_label({"record_type": "message", "source_type": "ai", "human_text": False,
                          "ai_output": True}) == "Assistant"


def test_a_capability_call_is_never_the_user():
    # The live case: a cap_call stored with the dataclass default human_text=True.
    rec = {"record_type": "cap_call", "source_type": "tool", "human_text": True,
           "text": "[board.item.upsert] id=health-says-census-and-gpu-gate"}
    assert rc.role_label(rec) == "Tool call"
    assert rc.role_label({"record_type": "dag_step", "human_text": True}) == "Tool call"


def test_other_kinds_are_named_by_what_they_are():
    assert rc.role_label({"record_type": "dream", "human_text": True}) == "Dream"
    assert rc.role_label({"record_type": "dream_report"}) == "Dream"
    assert rc.role_label({"record_type": "event"}) == "Event"
    assert rc.role_label({"record_type": "observation"}) == "Observation"
    # an unknown type is named by its type, not by a chat role
    assert rc.role_label({"record_type": "skill_output", "human_text": True}) == "Skill output"


def test_imported_agent_transcript_lines_are_marked():
    rec = {"record_type": "message", "source_type": "human", "human_text": True,
           "text": "[Claude Code · --llm-int-boejaker-Vera] Watching for the final gate."}
    assert rc.role_label(rec) == "Agent transcript"
    assert rc.role_label({"record_type": "message", "text": "[Codex] done"}) == "Agent transcript"


def test_a_message_without_a_source_falls_back_to_the_flag():
    assert rc.role_label({"record_type": "message", "human_text": True}) == "User"
    assert rc.role_label({"record_type": "message", "human_text": False}) == "Assistant"
    assert rc.role_label({}) == "User"          # a bare legacy row reads as before


def test_role_label_reads_objects_too():
    class Rec:
        record_type = "cap_call"
        source_type = "tool"
        human_text = True
        text = "[x]"
    assert rc.role_label(Rec()) == "Tool call"


# ── what recall leaves out (owner, 2026-10-04) ────────────────────────────────
def test_exclusions_parse_and_reject_junk():
    assert rc.parse_exclusions("cap_call,dag_step,transcript") == (("cap_call", "dag_step"), True)
    assert rc.parse_exclusions("") == ((), False)
    assert rc.parse_exclusions(" Cap_Call , x'); drop table --, transcript") == (("cap_call",), True)


def test_default_filters_leave_activity_out():
    f = rc.default_recall_filters({"session_id": "s"})
    assert f == {"session_id": "s", "exclude_record_types": ["cap_call", "dag_step"], "exclude_transcripts": True}
    assert rc.default_recall_filters(None) == {"exclude_record_types": ["cap_call", "dag_step"],
                                               "exclude_transcripts": True}


def test_asking_for_activity_gets_everything():
    assert rc.default_recall_filters({"include_activity": True, "tags": ["a"]}) == {"tags": ["a"]}
    assert rc.default_recall_filters({"record_type": "cap_call"}) == {"record_type": "cap_call"}


def test_an_empty_policy_changes_nothing():
    assert rc.default_recall_filters({"q": 1}, types=(), transcripts=False) == {"q": 1}


def test_default_filters_do_not_mutate_the_callers_dict():
    mine = {"include_activity": True}
    rc.default_recall_filters(mine)
    assert mine == {"include_activity": True}


def test_embed_worthy():
    assert rc.embed_worthy("message", "how is the gpu gate configured")
    assert not rc.embed_worthy("cap_call", "[board.item.upsert] ...")
    assert not rc.embed_worthy("message", "[Claude Code · x] Watching for the final gate.")
    assert rc.embed_worthy("cap_call", "x", types=(), transcripts=False)


# ── content_key / collapse_repeats ────────────────────────────────────────────
def _hit(rid, text, score):
    return {"record": {"id": rid, "text": text}, "score": score}


def test_same_text_different_spacing_and_case_share_a_key():
    a = rc.content_key({"id": "1", "text": "Watching for the final gate."})
    b = rc.content_key({"id": "2", "text": "  watching for the   final gate. "})
    assert a == b
    assert rc.content_key({"id": "3", "text": "Watching for the first gate."}) != a


def test_records_without_text_never_collapse_together():
    assert rc.content_key({"id": "1", "text": ""}) != rc.content_key({"id": "2", "text": ""})


def test_collapse_keeps_the_first_copy_and_counts_the_rest():
    hits = [
        _hit("a", "Watching for the final gate.", 7.9),
        _hit("b", "Adding the rule.", 7.7),
        _hit("c", "Watching for the final gate.", 7.8),
        _hit("d", "Adding the rule.", 7.6),
        _hit("e", "Something else.", 7.0),
    ]
    out = rc.collapse_repeats(hits)
    assert [h["record"]["id"] for h in out] == ["a", "b", "e"]
    assert out[0]["repeats"] == 2 and out[0]["repeat_ids"] == ["c"]
    assert out[1]["repeats"] == 2 and out[1]["repeat_ids"] == ["d"]
    assert out[2]["repeats"] == 1 and "repeat_ids" not in out[2]


def test_limit_counts_distinct_texts():
    hits = [_hit(str(i), "same", 1.0) for i in range(5)] + [_hit("x", "other", 0.5), _hit("y", "third", 0.4)]
    out = rc.collapse_repeats(hits, limit=2)
    assert [h["record"]["id"] for h in out] == ["0", "x"]
    assert out[0]["repeats"] == 5


def test_counts_add_up_across_two_passes():
    first = rc.collapse_repeats([_hit("a", "same", 2.0), _hit("b", "same", 1.0), _hit("c", "same", 0.5)])
    assert first[0]["repeats"] == 3
    # a second pass (the chat block) meets that hit and one more copy from the session's own set
    again = rc.collapse_repeats(first + [{"record": {"id": "d", "text": "SAME"}, "score": 0.1}])
    assert len(again) == 1 and again[0]["repeats"] == 4
    assert sorted(again[0]["repeat_ids"]) == ["b", "c", "d"]


def test_collapse_accepts_flat_items_and_does_not_mutate_input():
    items = [{"id": "1", "text": "hello", "score": 1.0}, {"id": "2", "text": "Hello", "score": 0.9}]
    out = rc.collapse_repeats(items)
    assert len(out) == 1 and out[0]["repeats"] == 2
    assert "repeats" not in items[0]


# ── fast_then_vector: one budget for a deep memory search (owner: 1.5 s, 2026-10-04) ──────────────────────────
import asyncio  # noqa: E402


def _arun(coro):
    """Run a coroutine on a private loop. asyncio.run() would clear the thread's current loop when it ends, and a
    later test calling asyncio.get_event_loop() then fails (Python 3.11) - order-dependent failures elsewhere."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _two_tier(slow_after, slow_result=("fast", "vec"), fast=("fast",)):
    """A fake search_fast_and_slow: fast hits at once, the slow task resolves after `slow_after` seconds."""
    made = {}

    async def search(fast_timeout):
        async def slow():
            await asyncio.sleep(slow_after)
            return list(slow_result)
        made["task"] = asyncio.get_running_loop().create_task(slow())
        made["fast_timeout"] = fast_timeout
        return list(fast), made["task"]
    return search, made


def test_the_vector_tier_is_used_when_it_lands_inside_the_budget():
    search, made = _two_tier(0.01)
    hits, vector = _arun(rc.fast_then_vector(search, 0.5))
    assert hits == ["fast", "vec"] and vector is True
    assert made["fast_timeout"] == 0.5


def test_past_the_budget_the_text_hits_stand_and_the_vector_task_is_cancelled():
    async def go():
        search, made = _two_tier(5.0)
        out = await rc.fast_then_vector(search, 0.2)
        await asyncio.sleep(0)
        return out, made["task"]
    (hits, vector), task = _arun(go())
    assert hits == ["fast"] and vector is False
    assert task.cancelled()


def test_a_failing_search_is_an_empty_answer_never_an_error():
    async def boom(fast_timeout):
        raise RuntimeError("postgres down")
    assert _arun(rc.fast_then_vector(boom, 0.5)) == ([], False)


def test_a_failing_vector_tier_keeps_the_text_hits():
    async def search(fast_timeout):
        async def slow():
            raise RuntimeError("embed node gone")
        return ["fast"], asyncio.get_running_loop().create_task(slow())
    assert _arun(rc.fast_then_vector(search, 0.5)) == (["fast"], False)


def test_no_slow_tier_means_the_fast_hits():
    async def search(fast_timeout):
        return ["a", "b"], None
    assert _arun(rc.fast_then_vector(search, 0.5)) == (["a", "b"], False)


def test_retitle_block_keeps_the_lines_and_swaps_the_heading():
    block = "=== Relevant memories from past conversations ===\n[2026-10-01 10:00] User: the plan"
    out = rc.retitle_block(block, "=== Earlier conversations ===")
    assert out == "=== Earlier conversations ===\n[2026-10-01 10:00] User: the plan"
    assert rc.retitle_block("", "X") == "" and rc.retitle_block("only a heading", "X") == ""

# ── gather_within: one slow backend does not throw away the other's hits ─────────────────────────────────────────
def test_what_finished_in_time_is_kept_and_the_slow_one_cancelled():
    async def go():
        async def fast():
            return ["pg-hit"]
        async def slow():
            await asyncio.sleep(5)
            return ["never"]
        return await rc.gather_within({"postgres": fast(), "neo4j": slow()}, 0.2)
    out = _arun(go())
    assert out[0] == ["pg-hit"]
    assert isinstance(out[1], TimeoutError) and isinstance(out[1], Exception)


def test_a_failed_backend_stands_as_its_exception_in_order():
    async def go():
        async def boom():
            raise RuntimeError("neo4j down")
        async def ok():
            return ["n"]
        return await rc.gather_within({"neo4j": boom(), "postgres": ok()}, 1.0)
    out = _arun(go())
    assert isinstance(out[0], RuntimeError) and out[1] == ["n"]


def test_gather_within_nothing_is_nothing():
    assert _arun(rc.gather_within({}, 1.0)) == []


def test_fast_then_vector_says_how_the_text_tier_went():
    search, _made = _two_tier(5.0)
    info = {}
    _arun(rc.fast_then_vector(search, 0.2, info=info))
    assert info["fast_hits"] == 1 and info["fast_ms"] >= 0 and "error" not in info

    async def boom(fast_timeout):
        raise RuntimeError("postgres down")
    info2 = {}
    _arun(rc.fast_then_vector(boom, 0.2, info=info2))
    assert info2["fast_hits"] == 0 and "postgres down" in info2["error"]