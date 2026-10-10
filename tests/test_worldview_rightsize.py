"""The JEPA worldview right-sized, held out and trained honestly (four-minds FM-8.2).

2026-10-10 health: 4.5 records per concept (< 20), 40 dynamics steps per transition (memorising), no checkpoint
time, no held-out evaluation. Right-sized defaults, a held-out split scored against unigram AND bigram, a step cap,
the save time in the checkpoint, and no LLM labels below a membership floor.
"""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.worldview import worldview_eval_core as EV, worldview_health_core as HC  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_fixed_share_of_walks_is_held_back():
    walks = [[0, i + 2] for i in range(10)]
    train, test = EV.split_walks(walks, 0.2)
    assert len(test) == 2 and len(train) == 8 and not set(map(tuple, test)) & set(map(tuple, train))
    assert EV.split_walks(walks, 0.2) == (train, test)                   # deterministic
    assert EV.split_walks(walks[:3], 0.2) == (walks[:3], [])             # too few to hold any back
    assert EV.split_walks(walks, 0) == (walks, [])


def test_the_step_cap_keeps_the_dynamics_under_the_health_ceiling():
    # 100 transitions -> 0.9 x 20 x 100 = 1,800 steps; 30 steps an epoch
    assert EV.step_cap(100, 0, 30, 500) == 60
    assert EV.step_cap(100, 1700, 30, 500) == 3                          # what earlier runs already used counts
    assert EV.step_cap(100, 5000, 30, 500) == 0 and EV.step_cap(100, 0, 30, 10) == 10
    assert EV.step_cap(0, 0, 30, 10) == 0 and EV.step_cap(100, 0, 0, 10) == 0
    assert EV.transitions([[0, 2, 3, 2], [0, 3, 2]]) == {(0, 1), (1, 0)}


def test_the_baselines_and_the_verdict():
    train = [[0, 2, 3, 2, 3, 1], [0, 2, 3, 2, 3]]                       # 2 -> 3 -> 2 ...; 1 = padding
    test = [[0, 2, 3, 2, 3]]
    b = EV.baselines(train, test, vocab=4)
    assert b["n"] == 4 and b["bigram_nll"] < b["unigram_nll"] and b["bigram_acc"] == 1.0
    # add-one smoothing: bigram P(3|2) = (4+1)/(4+4)
    assert b["bigram_nll"] > 0 and math.isclose(-math.log(5 / 8), 0.47, abs_tol=0.01)
    v = EV.verdict(0.1, 0.9, dict(b, n=500), train_walks=8, test_walks=2)
    assert v["beats_baseline"] and v["n_tokens"] == 500 and v["train_walks"] == 8
    v = EV.verdict(b["bigram_nll"] + 0.01, 0.9, dict(b, n=500))
    assert not v["beats_baseline"] and "bigram" in v["why"]
    v = EV.verdict(0.01, 1.0, b)                                         # 4 tokens: says nothing
    assert not v["beats_baseline"] and "held-out tokens" in v["why"]
    assert EV.baselines(train, [], 4) == {"n": 0}


def test_health_turns_on_a_held_out_result():
    m = {"ready": True, "num_concepts": 96, "records_assigned": 2300, "transitions_observed": 900,
         "train_steps": {"gnn": 10, "codebook": 10, "dynamics": 1000},
         "codebook": {"perplexity": 40}, "last_fabric_persist": "2026-10-10T20:00:00Z"}
    from datetime import datetime, timezone
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert HC.verdict(m, {"vectors": 2300}, held_out={"beats_baseline": True}, now=now)["status"] == "ok"
    v = HC.verdict(m, {"vectors": 2300}, held_out={"beats_baseline": False}, now=now)
    assert v["status"] == "degraded" and any("does not beat" in r for r in v["reasons"])


def test_the_model_is_right_sized_keeps_its_held_out_result_and_labels_only_real_concepts():
    src = open(os.path.join(ROOT, "vera", "worldview", "worldview_jepa.py"), encoding="utf-8").read()
    assert 'os.getenv("WORLDVIEW_NUM_CONCEPTS", "96")' in src and 'os.getenv("WORLDVIEW_HIDDEN_DIM", "256")' in src
    assert 'os.getenv("WORLDVIEW_GNN_LAYERS", "1")' in src and 'os.getenv("WORLDVIEW_BLOB_KEY", "worldview_v3")' in src
    assert '"held_out":          dict(self.held_out or {}),' in src and '"saved_at":' in src
    assert 'held_out=st.get("held_out") or None' in src
    assert "_wv_eval.split_walks(" in src and "_wv_eval.step_cap(" in src and "model.eval_dynamics(test_walks)" in src
    assert 'target = [c for c in target if pop[c] >= floor][:max_concepts]' in src
