"""Routing that knows what is loaded and how many slots a node has.

2026-09-28: the load-aware picker scored raw `in_use`, so a CPU node serving
one of its TWO slots looked as busy as a GPU node with its only slot taken,
and it gave nothing for a model already being resident - a cold 35b on CPU
costs 62 s, the 9b 10.5 s. It also dropped the base picker's context
escalation (`ctx_need` arrived in **kwargs and was ignored), so a prompt too
big for the GPU window went to the GPU anyway and spilled.

Pure arithmetic here; the picker wiring is exercised in the app tests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.workers import route_preference as RP      # noqa: E402

pytestmark = pytest.mark.critical

M = "jaahas/qwen3.5-uncensored"


def test_resident_matches_latest_alias():
    assert RP.is_resident(M, [M + ":latest"])
    assert RP.is_resident(M + ":latest", [M])
    assert not RP.is_resident("qwen2.5:7b", ["qwen2.5:0.5b"])
    assert not RP.is_resident("", [M])


def test_prefer_still_beats_warmth_while_idle_and_yields_when_busy():
    """The whole point of the sizes: WARM_BONUS < PREFER_BONUS."""
    assert RP.WARM_BONUS < RP.PREFER_BONUS
    preferred_idle_cold = 0 + RP.preference_bonus("a", "a") + RP.warm_bonus(M, [])
    other_idle_warm = 0 + RP.preference_bonus("b", "a") + RP.warm_bonus(M, [M])
    assert preferred_idle_cold < other_idle_warm
    preferred_busy_cold = 1 + RP.preference_bonus("a", "a") + RP.warm_bonus(M, [])
    assert other_idle_warm < preferred_busy_cold


def test_warm_breaks_a_tie_between_idle_nodes():
    assert RP.warm_bonus(M, [M]) < RP.warm_bonus(M, ["other"])


def test_saturation_counts_slots():
    assert RP.saturation_penalty(1, 1) == RP.FULL_PENALTY      # GPU, its one slot taken
    assert RP.saturation_penalty(1, 2) == 0.0                  # CPU, one of two
    assert RP.saturation_penalty(2, 2) == RP.FULL_PENALTY
    assert RP.saturation_penalty(0, 0) == 0.0
    # a full node loses to a half-used one even with a preference
    full = 2 + RP.saturation_penalty(2, 2) + RP.preference_bonus("a", "a")
    half = 1 + RP.saturation_penalty(1, 2)
    assert half < full


CANDS = {
    "gpu-250": {"has_gpu": True, "in_use": 1, "running": [M]},
    "gpu-250-cpu": {"has_gpu": False, "in_use": 0, "running": [M, "nomic-embed-text:latest"]},
    "cpu-246": {"has_gpu": False, "in_use": 0, "running": ["qwen2.5:0.5b"]},
    "cpu-247": {"has_gpu": False, "in_use": 2, "running": [M]},
}


def _slots(iid, inst):
    return 1 if inst.get("has_gpu") else 2


def test_spill_only_to_warm_free_proven_nodes():
    tps = {"gpu-250-cpu": 3.86, "cpu-247": 9.0}
    sp = RP.spill_candidates(CANDS, M, _slots, lambda i: tps.get(i, 0), 3.0, False)
    # cpu-246 is cold, cpu-247 is full, the GPU is not a spill target
    assert sorted(sp) == ["gpu-250-cpu"]


def test_spill_refused_when_too_slow_unless_a_scenario_says_so():
    slow = {"gpu-250-cpu": 0.08}
    assert RP.spill_candidates(CANDS, M, _slots, lambda i: slow.get(i, 0), 3.0, False) == {}
    assert sorted(RP.spill_candidates(CANDS, M, _slots, lambda i: 0.0, 3.0, True)) == ["gpu-250-cpu"]


def test_spill_refused_for_a_big_prompt():
    """A CPU prompt-eval of a big context is slower than waiting for the GPU."""
    assert RP.spill_candidates(CANDS, M, _slots, lambda i: 9.0, 3.0, True,
                               ctx_need=20000, max_ctx=8192) == {}
