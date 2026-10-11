"""The card-similarity shadow judge (four-minds FM-2.1 stage-0): one card per label, the nearest card wins, recorded
as card:minilm beside the live decision and queued nightly after the kNN judge."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import judge_core as JC  # noqa: E402

pytestmark = pytest.mark.critical


def test_every_question_has_a_card_per_label():
    for qid, q in JC.QUESTIONS.items():
        assert set(JC.CARDS[qid]) == set(q["labels"].values()), qid
        assert all(len(t) > 20 for t in JC.CARDS[qid].values())


def test_the_nearest_card_wins_with_a_softmax_p():
    cards = {"build": [1.0, 0.0, 0.0], "research": [0.0, 1.0, 0.0], "action": [0.0, 0.0, 1.0]}
    lab, p = JC.card_vote([0.9, 0.1, 0.0], cards)
    assert lab == "build" and 0.5 < p <= 1.0
    lab2, p2 = JC.card_vote([0.5, 0.5, 0.0], cards)
    assert lab2 in ("build", "research") and p2 < p
    assert JC.card_vote(None, cards) == (None, None) and JC.card_vote([1.0], {}) == (None, None)


def test_the_judge_is_recorded_and_queued_nightly():
    src = open(os.path.join(ROOT, "vera", "minds", "judge.py"), encoding="utf-8").read()
    assert '"decide.shadow.cards"' in src and "JC.shadow_decision(live, answer, p, JC.CARD_IMPL" in src
    night = src[src.index("async def _night_job("):]
    assert night.index('"decide.shadow.knn"') < night.index('"decide.shadow.cards"')
    assert JC.CARD_IMPL == "card:minilm"
