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
    # B3: the night's jobs come from the bindings; the card judge still runs after kNN, whose vectors it reads
    from vera.minds import judge as J, shadow_plan_core as SP
    caps = [c for c, *_x in SP.plan(SP.judge_bindings(J._orch.CAPABILITY_REGISTRY), list(JC.QUESTIONS), ["m"], 1)]
    assert caps.index("decide.shadow.knn") < caps.index("decide.shadow.cards")
    assert JC.CARD_IMPL == "card:minilm"


def test_the_card_judge_is_bound_and_vectors_say_what_made_them():
    # backbone review 11 Oct: decide.shadow.cards had no serves= binding (B3 picks candidates from bindings), and
    # cached MiniLM vectors carried no model or dimension (W2-05/W2-06)
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "vera", "minds", "judge.py"), encoding="utf-8").read()
    cards = src[src.index("_SERVES_CARDS = _bind("):src.index("_SERVES_KNN = _bind(")]
    assert '"nlp"' in cards and "tier=3" in cards and 'placement="nlp"' in cards and "JC.CARD_IMPL" in cards
    assert '"decide.shadow.cards", serves=_SERVES_CARDS' in src
    assert "ADD COLUMN IF NOT EXISTS model TEXT" in src and "ADD COLUMN IF NOT EXISTS dim INTEGER" in src
    assert "INSERT INTO decision_vectors (decision_id, question_id, vec, model, dim)" in src
    sense = open(_os.path.join(root, "vera", "minds", "sense_capabilities.py"), encoding="utf-8").read()
    assert '"vectors": cards}' in sense and 'isinstance(got.get("vectors"), dict)' in sense
    canvas = open(_os.path.join(root, "vera", "canvas", "enrich_capabilities.py"), encoding="utf-8").read()
    assert '"model": _EMBED_ID["model"], "dim": _EMBED_ID["dim"]' in canvas
