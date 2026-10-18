"""Intelligence decisions and outcomes in the frozen four-minds shapes (Decision v0 /
Outcome v0, ROADMAP §5.1): every field present, pointers not payloads, raw scores never
passed off as probabilities, outcomes labelled by whether the DECISION was right."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.intel import intel_ledger_core as L  # noqa: E402

DECISION_FIELDS = {"schema", "decision_id", "ts", "origin", "surface", "question_id", "qtype", "labels", "labels_hash",
                   "state_hash", "state_ref", "answer", "p", "margin", "decider", "implementation", "package", "node",
                   "ms", "mode", "shadow", "minds_digest", "run_id", "session_id", "turn_id", "detail"}
OUTCOME_FIELDS = {"schema", "decision_id", "ts", "label", "source", "strength", "detail"}


def _dec(answer="relevant"):
    return L.make_decision("intel.signal_relevant", answer, state={"url": "https://a", "title": "T"},
                           state_ref={"kind": "signal", "id": "abc"}, detail={"score": 7.2, "text": "x" * 500})


def test_decision_v0_shape():
    d = _dec()
    assert set(d) == DECISION_FIELDS
    assert d["schema"] == "vera.decision/v0" and d["decision_id"].startswith("dec_") and len(d["decision_id"]) == 36
    assert d["origin"] == d["surface"] == "intel" and d["qtype"] == "binary"
    assert len(d["labels_hash"]) == 12 and len(d["state_hash"]) == 16
    assert d["p"] is None and d["shadow"] is None and d["minds_digest"] == "default"
    assert d["detail"] == {"score": 7.2}                    # long text never enters a row
    assert d["labels_hash"] == L.labels_hash(reversed(d["labels"]))


def test_outcomes_say_whether_the_decision_was_right():
    rel, notrel = _dec("relevant"), _dec("not_relevant")
    o = L.make_outcome(rel, "user.feedback.useful")
    assert set(o) == OUTCOME_FIELDS and o["schema"] == "vera.outcome/v0"
    assert o["label"] == "correct" and o["strength"] == "strong"
    assert L.make_outcome(notrel, "user.feedback.useful")["label"] == "wrong"      # it was worth it after all
    assert L.make_outcome(rel, "user.feedback.mute_site")["label"] == "wrong"
    assert L.make_outcome(rel, "link.opened")["strength"] == "medium"
    assert L.make_outcome(rel, "made.up") is None


def test_strongest_and_agreement():
    d = _dec()
    weak = L.make_outcome(d, "link.opened", ts="2026-10-04T10:00:00Z")
    strong = L.make_outcome(d, "user.feedback.not_useful", ts="2026-10-04T09:00:00Z")
    assert L.strongest([weak, strong])["source"] == "user.feedback.not_useful"
    rows = [dict(d, outcomes=[weak, strong]), dict(_dec(), outcomes=[]),
            dict(_dec(), outcomes=[L.make_outcome(_dec(), "user.feedback.useful")])]
    a = L.agreement(rows)["intel.signal_relevant"]
    assert a == {"decisions": 3, "with_outcome": 2, "correct": 1, "precision": 0.5}


def test_cross_site_duplication_is_not_an_outcome():
    """Syndicated generic pages are copied across sites; that is not evidence the item was relevant."""
    assert "story.confirmed" not in L.EVENTS
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "vera", "intel", "intel_capabilities.py"), encoding="utf-8").read()
    assert "story.confirmed" not in src and "intel.outcomes.backfill" not in src


def test_signal_inputs_carry_what_a_rater_needs_and_keep_the_hashed_state():
    # FM-13.6: the training store gets the topic's name and the text; the ledger row still carries only the hash
    sig = {"id": "s1", "url": "https://a", "title": "T", "score": 7, "genre": "news", "novelty": 0.4,
           "snippet": "short", "summary": "s" * 5000, "site": "a.example"}
    inp = L.signal_inputs(sig, {"id": "t1", "name": "Vendors"})
    assert {k: inp[k] for k in L.signal_state(sig, "t1")} == L.signal_state(sig, "t1")
    assert inp["topic_name"] == "Vendors" and inp["source"] == "a.example"
    assert inp["snippet"] == "s" * L.SNIPPET_CHARS                  # the summary wins over the snippet, bounded
    assert L.signal_inputs({"snippet": "x"}, {})["snippet"] == "x"
    d = L.make_decision("intel.signal_relevant", "relevant", state=L.signal_state(sig, "t1"),
                        state_ref={"kind": "signal", "id": "s1"})
    assert "snippet" not in json.dumps(d) and d["surface"] == "intel"
