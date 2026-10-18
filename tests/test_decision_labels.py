"""A person labels recorded decisions (four-minds FM-7.0 labelling view, for every question)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import label_core as LB, ledger_core as LC, loop_decisions_core as LD  # noqa: E402

pytestmark = pytest.mark.critical


def _intent(answer="build"):
    return LD.intent_decision("sess", answer, {"heuristic": answer, "llm": ""})


def test_a_verdict_is_a_strong_human_outcome():
    d = _intent()
    o = LB.human_outcome(d, verdict="wrong")
    assert LC.check_outcome(o) == []
    assert (o["label"], o["source"], o["strength"]) == ("wrong", "human.label", "strong")
    assert o["decision_id"] == d["decision_id"]


def test_the_right_answer_decides_the_verdict_and_must_be_a_label():
    d = _intent("build")
    assert LB.human_outcome(d, right_answer="build")["label"] == "correct"
    o = LB.human_outcome(d, right_answer="research")
    assert o["label"] == "wrong" and o["detail"]["right_answer"] == "research"
    with pytest.raises(ValueError):
        LB.human_outcome(d, right_answer="cooking")
    with pytest.raises(ValueError):
        LB.human_outcome(d, verdict="maybe")


def test_disagreements_come_first_then_newest():
    live = lambda a: {"answer": a}
    items = [{"row": live("build"), "shadows": [], "ts": "2026-10-08T10:00:00"},
             {"row": live("build"), "shadows": [{"answer": "research"}], "ts": "2026-10-07T10:00:00"},
             {"row": live("build"), "shadows": [], "ts": "2026-10-08T12:00:00"},
             {"row": live("build"), "shadows": [{"answer": "build"}], "ts": "2026-10-06T10:00:00"}]
    got = [it["ts"][:13] for it in LB.order(items, 10)]
    assert got == ["2026-10-07T10", "2026-10-08T12", "2026-10-08T10", "2026-10-06T10"]
    assert len(LB.order(items, 2)) == 2


def test_the_view_carries_what_the_page_shows():
    d = _intent("build")
    v = LB.view({"row": d, "state": {"goal": "make a timer"}, "ts": "t",
                 "shadows": [{"answer": "research", "implementation": "nlp.zeroshot", "p": 0.8}], "outcomes": []})
    assert v["labels"] == LD.INTENT_LABELS and v["state"] == {"goal": "make a timer"}
    assert v["disagreement"] is True and v["shadows"][0]["implementation"] == "nlp.zeroshot"


def test_the_page_and_caps_are_wired():
    src = open(os.path.join(ROOT, "vera", "minds", "label_capabilities.py"), encoding="utf-8").read()
    assert '"decide.label.queue"' in src and '"decide.label"' in src and '@APP.get("/decide/labels"' in src
    assert os.path.exists(os.path.join(ROOT, "vera", "minds", "labels_panel.html"))
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert 'os.path.join(_here, "minds/label_capabilities.py"),' in orch
    page = open(os.path.join(ROOT, "vera", "minds", "labels_panel.html"), encoding="utf-8").read()
    assert "/decide/label/queue" in page and "fetch('/decide/label'" in page


# -- D10-4 (18 Oct): each shadow on the decision it answered ------------------------------------------------------
def test_shadows_attach_to_the_decision_they_answered_not_the_whole_run():
    from vera.minds import label_core as LB
    decs = [{"decision_id": "d1", "run_id": "r", "question_id": "output.claim_supported"},
            {"decision_id": "d2", "run_id": "r", "question_id": "output.claim_supported"}]
    sh = [{"run_id": "r", "question_id": "output.claim_supported", "row": {"answer": "supported", "detail": {"for": "d1"}}},
          {"run_id": "r", "question_id": "output.claim_supported", "row": {"answer": "unsupported", "detail": {"for": "d2"}}},
          {"run_id": "r", "question_id": "output.claim_supported", "row": {"answer": "x", "detail": {"for": "elsewhere"}}}]
    got = LB.shadows_for(decs, sh)
    assert [s["answer"] for s in got["d1"]] == ["supported"] and [s["answer"] for s in got["d2"]] == ["unsupported"]


def test_older_shadows_without_for_still_fall_back_to_their_run():
    from vera.minds import label_core as LB
    decs = [{"decision_id": "d1", "run_id": "r", "question_id": "loop.tier"},
            {"decision_id": "d9", "run_id": "other", "question_id": "loop.tier"}]
    sh = [{"run_id": "r", "question_id": "loop.tier", "row": {"answer": "B", "detail": {}}}]
    got = LB.shadows_for(decs, sh)
    assert len(got["d1"]) == 1 and got["d9"] == []


def test_the_queue_and_the_ui_use_it():
    src = open(os.path.join(ROOT, "vera", "minds", "label_capabilities.py"), encoding="utf-8").read()
    assert "LB.shadows_for(" in src and "row->'detail'->>'for' = ANY($1::text[])" in src
    assert 'shadows.get(r["decision_id"], [])' in src and "(r[\"run_id\"], r[\"question_id\"])" not in src
    assert 'register_ui(\n    "decision-labels"' in src and '<iframe src="/decide/labels"' in src and 'mode="inject"' in src
