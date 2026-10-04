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
