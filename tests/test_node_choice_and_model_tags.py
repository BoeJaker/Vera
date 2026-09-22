"""One CPU node did 90% of the work, and a 0.5b model counted as a 7b.

Two faults in the router that REPLACES `pick_instance`
(`vera/workers/cluster.py`), both found 2026-09-22 from census ledgers.

1. The base `pick_instance` breaks ties least-recently-used, because a stream
   of one-at-a-time requests sees `in_use == 0` everywhere and otherwise picks
   the same node forever. The replacement never had that term. Embeddings per
   census, cpu-246 / cpu-247:  run59 151/63, run60 318/22, run61 235/21.

2. Both copies of `_has_model` matched the BASE name, so `qwen2.5:7b` was
   "served" by a node holding only `qwen2.5:0.5b`. Every node carries both tags
   today, which is the only reason it has not bitten.

Pure: dicts in, decisions out. No cluster, no Ollama.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera import model_tag_core as MT                    # noqa: E402
from vera.workers import node_choice as NC               # noqa: E402

pytestmark = pytest.mark.critical

#: What every node in the estate actually lists (ollama.instances, 22 Sep).
SERVED = ["jaahas/qwen3.5-uncensored:9b", "jaahas/qwen3.5-uncensored:latest",
          "nomic-embed-text:latest", "qwen2.5:0.5b", "qwen2.5:7b"]


# -- the tie-break ------------------------------------------------------------
#: The two CPU nodes as the estate actually configures them: same load, and
#: the static priorities that used to decide every request.
PRIO = {"cpu-246": 1, "cpu-247": 2}


def test_equally_idle_nodes_take_turns():
    """The census case: twenty embeds, both nodes idle."""
    scores = {"cpu-246": 0.0, "cpu-247": 0.0}
    last = {}
    picks = []
    for i in range(20):
        c = NC.choose(scores, last, PRIO)
        picks.append(c)
        NC.record(last, c, float(i))
    assert picks.count("cpu-246") == 10 and picks.count("cpu-247") == 10, picks
    assert picks[0] == "cpu-246"          # priority still orders the cold start
    assert picks[1] == "cpu-247"          # and the peer is tried immediately


def test_priority_no_longer_monopolises_two_idle_nodes():
    """The actual bug. `priority * 0.01` was folded into the score, so two
    idle nodes differed on EVERY request and the fairness term never ran.
    Priority now breaks only a tie that survives fairness."""
    old_style = {"cpu-246": 0.0 + 1 * 0.01, "cpu-247": 0.0 + 2 * 0.01}
    last = {}
    monopoly = []
    for i in range(6):
        c = NC.choose(old_style, last)     # priority baked into the score
        monopoly.append(c)
        NC.record(last, c, float(i))
    assert set(monopoly) == {"cpu-246"}    # this is what shipped

    last = {}
    fair = []
    for i in range(6):
        c = NC.choose({"cpu-246": 0.0, "cpu-247": 0.0}, last, PRIO)
        fair.append(c)
        NC.record(last, c, float(i))
    assert fair == ["cpu-246", "cpu-247"] * 3


def test_load_still_beats_fairness():
    """Fairness is below load - a busy node is never chosen to be fair."""
    last = {"cpu-246": 100.0, "cpu-247": 1.0}       # 247 waited far longer
    assert NC.choose({"cpu-246": 0.0, "cpu-247": 1.0}, last, PRIO) == "cpu-246"


def test_a_soft_preference_still_beats_fairness():
    """route_preference's bonus is a deliberate 'favours, but will yield' and
    must keep outranking the round-robin."""
    last = {"cpu-247": 0.0, "cpu-246": 999.0}
    assert NC.choose({"cpu-246": 0.0, "cpu-247": 0.5}, last, PRIO) == "cpu-246"


def test_a_node_never_picked_goes_first():
    """It has been waiting since before the record began. Defaulting an absent
    stamp to 0.0 instead made a fresh peer merely TIE with a node picked at
    t=0, so the first two requests both went to the higher-priority node."""
    last = {"cpu-246": 500.0}
    assert NC.choose({"cpu-246": 0.0, "cpu-247": 0.0}, last, PRIO) == "cpu-247"
    assert NC.choose({"cpu-246": 0.0, "cpu-247": 0.0}, {"cpu-246": 0.0}, PRIO) == "cpu-247"


def test_choice_is_deterministic_when_nothing_distinguishes_them():
    assert NC.choose({"b": 0.0, "a": 0.0}, {}) == "a"
    assert NC.choose({}, {}) == ""


def test_tuple_rankings_are_accepted():
    """The caller may rank on more than one number before fairness."""
    assert NC.choose({"a": (1, 0.0), "b": (0, 9.0)}, {}) == "b"


def test_record_shares_one_mapping():
    """The router hands in the orchestrator's own _LAST_PICKED so both routers
    keep one record of who was last used."""
    shared = {}
    out = NC.record(shared, "cpu-246", 12.0)
    assert out is shared and shared == {"cpu-246": 12.0}
    assert NC.record(None, "x", 1.0) == {"x": 1.0}
    assert NC.record({}, "", 1.0) == {}


# -- exact tags ---------------------------------------------------------------
def test_a_smaller_tag_does_not_serve_a_bigger_one():
    """The fault: base-name matching made these one model."""
    assert not MT.same_model("qwen2.5:7b", "qwen2.5:0.5b")
    assert not MT.is_served("qwen2.5:7b", ["qwen2.5:0.5b"])
    assert MT.is_served("qwen2.5:7b", SERVED)


def test_bare_and_latest_are_the_same_model():
    """The one equivalence Ollama itself applies."""
    assert MT.same_model("qwen2.5", "qwen2.5:latest")
    assert MT.is_served("jaahas/qwen3.5-uncensored", SERVED)        # via :latest
    assert MT.is_served("jaahas/qwen3.5-uncensored:latest", SERVED)
    assert MT.is_served("jaahas/qwen3.5-uncensored:9b", SERVED)


def test_a_repository_path_is_not_mistaken_for_a_tag():
    assert MT.variants("jaahas/qwen3.5-uncensored") == {
        "jaahas/qwen3.5-uncensored", "jaahas/qwen3.5-uncensored:latest"}
    assert not MT.same_model("jaahas/qwen2.5:7b", "library/qwen2.5:7b")


def test_no_model_asked_for_is_served_by_anyone():
    """How the callers use it: an empty model means no preference."""
    assert MT.is_served("", SERVED) and MT.is_served(None, [])
    assert not MT.same_model("", "qwen2.5")


def test_the_operator_repair_uses_the_same_rule():
    """A name that routes must not be a name the operator's repair rejects."""
    from vera.dag import operator_model_arg_core as OM
    assert OM.model_is_served("jaahas/qwen3.5-uncensored", SERVED)
    assert not OM.model_is_served("qwen2.5", SERVED)     # only :0.5b and :7b exist
    assert OM.heal_model_arg("operator.run", {"model": "qwen2.5:7b"}, SERVED) == []
    assert OM.heal_model_arg("operator.run", {"model": "fast-preview"}, SERVED)
