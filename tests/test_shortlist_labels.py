"""FM-3.5 (four-minds): every (offered, used, ok) triple a ranking label - loop steps and chat turns, failed calls
as negatives, exported for a learned ranker trained on outcomes."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC  # noqa: E402
from vera.minds import tool_shortlist_core as TC  # noqa: E402

pytestmark = pytest.mark.critical


def test_failed_calls_are_negative_outcomes_unless_the_tool_also_succeeded():
    d = TC.decision(["a", "b", "c"], run_id="r1", step_id=3)
    outs = TC.outcomes(d, used=["a", "z"], failed=["b", "a", "q"])
    got = sorted((o["label"], o["detail"]["tool"], o["detail"].get("rank")) for o in outs)
    assert got == [("failed", "b", 2), ("failed", "q", None), ("missed", "z", None), ("used", "a", 1)]
    assert all(LC.check_outcome(o) == [] for o in outs)
    assert [o["label"] for o in TC.outcomes(d, ["a"])] == ["used"]               # the old two-argument call still works


def test_a_chat_turn_is_the_same_question_on_the_chat_surface():
    d = TC.chat_decision(["web.search", "memory.seek"], session_id="s1", turn_id="t1")
    assert d["question_id"] == TC.QUESTION and d["surface"] == "chat" and d["origin"] == "chat"
    assert d["state_ref"] == {"kind": "chat_turn", "id": "s1:t1"} and d["mode"] == "shadow"
    assert LC.check_decision(d) == []


def test_a_ranking_example_labels_every_offered_used_and_failed_tool():
    d = TC.decision(["a", "b", "c"], run_id="r1", step_id=3)
    outs = TC.outcomes(d, used=["a", "z"], failed=["b"])
    e = TC.ranking_example(d, outs, text="find the latest rate")
    lab = {x["tool"]: (x["label"], x["why"], x["rank"]) for x in e["labels"]}
    assert lab == {"a": (1, "used", 1), "b": (0, "failed", 2), "c": (0, "offered_unused", 3),
                   "z": (1, "used_not_offered", None)}
    assert e["offered"] == ["a", "b", "c"] and e["used"] == ["a", "z"] and e["failed"] == ["b"]
    assert e["text"] == "find the latest rate" and e["surface"] == "loop" and e["group"] == "r1"
    # the split follows the run: every step of one goal on the same side
    d2 = TC.decision(["x"], run_id="r1", step_id=4)
    assert TC.ranking_example(d2, [])["split"] == e["split"]


def test_counts():
    d = TC.decision(["a"], run_id="r", step_id=1)
    c = TC.chat_decision(["a"], session_id="s", turn_id="t")
    ex = [TC.ranking_example(d, TC.outcomes(d, ["a"]), "t"), TC.ranking_example(c, TC.outcomes(c, [], ["a"]))]
    n = TC.ranking_counts(ex)
    assert n["examples"] == 2 and n["with_text"] == 1 and n["with_used"] == 1 and n["with_failed"] == 1
    assert n["by_surface"] == {"loop": 1, "chat": 1} and n["train"] + n["test"] == 2


def test_the_loop_and_the_chat_hand_their_turns_over():
    dag = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    assert 'h.get("ok") is False' in dag and "origin=origin, failed=failed)" in dag
    ag = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    body = ag[ag.index("async def _record_tools_shadow"):]
    body = body[:body.index("\nasync def ") if "\nasync def " in body[10:] else len(body)]
    assert "_tsm.on_chat_turn(session_id" in body
    ts = open(os.path.join(ROOT, "vera", "minds", "tool_shortlist.py"), encoding="utf-8").read()
    assert '"caps.shortlist.labels"' in ts and "ts.put(d, {\"text\": query})" in ts


def test_the_chat_surface_of_the_training_store_stays_off_by_default():
    from vera.minds import training_state_core as TS
    assert "chat" not in TS.enabled_surfaces(None)
