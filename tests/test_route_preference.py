"""One CPU node had all the generation work; the other only embedded.

Observed on prod 2026-09-08 during census 46: cpu-247 was cycling an 8.7GB
generation model while cpu-246 held only nomic-embed-text and sat at in_use=0.
Nothing was pinned. `avoid_embed: true` excludes whichever node hosts the embed
model, so for naming / summarize / dream_director the candidate list collapsed
to one before load was considered:

    deny_gpu -> ['cpu-246', 'cpu-247']
    avoid_embed: excluded 'cpu-246' -> ['cpu-247']
    least busy: picked 'cpu-247' (in_use=0) from ['cpu-247']

A hard exclusion cannot say "favours, but will yield", which is what was
actually wanted. Hence a soft preference - and the arithmetic below is the
whole point of it, so it is pinned here rather than left to a comment.

Pure: instance dicts in, decision out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.workers import route_preference as RP      # noqa: E402

pytestmark = pytest.mark.critical


def _score(iid, in_use, prefer):
    """The picker's score, reduced to the parts this module changes."""
    return in_use + RP.preference_bonus(iid, prefer)


# ── the preference must be soft, and that is arithmetic ─────────────────────
def test_the_preferred_node_wins_when_both_are_idle():
    assert _score("cpu-247", 0, "cpu-247") < _score("cpu-246", 0, "cpu-247")


def test_the_preferred_node_LOSES_when_it_is_the_busier_one():
    """Otherwise it is a pin with extra steps, and one node saturates again."""
    assert _score("cpu-247", 1, "cpu-247") > _score("cpu-246", 0, "cpu-247")


def test_the_preferred_node_wins_when_both_are_equally_busy():
    assert _score("cpu-247", 1, "cpu-247") < _score("cpu-246", 1, "cpu-247")


def test_the_bonus_is_smaller_than_one_in_flight_call():
    """The property the two tests above rest on: `in_use` moves in whole
    numbers, so a bonus of >= 1 would make the preference a hard pin."""
    assert 0 < RP.PREFER_BONUS < 1


def test_no_preference_means_no_adjustment():
    assert RP.preference_bonus("cpu-246", "") == 0.0
    assert RP.preference_bonus("cpu-246", None) == 0.0


def test_an_unpreferred_node_is_never_penalised():
    """The bonus only ever helps the preferred node; it must not push others
    up, or a preference would change the ordering of everything else too."""
    assert RP.preference_bonus("cpu-246", "cpu-247") == 0.0


# ── taking a free GPU, but only a free one ──────────────────────────────────
def _n(has_gpu, in_use, status="online"):
    return {"has_gpu": has_gpu, "in_use": in_use, "status": status}


def test_an_idle_gpu_is_offered():
    nodes = {"gpu-250": _n(True, 0), "cpu-246": _n(False, 0)}
    assert RP.free_gpu(nodes) == "gpu-250"


def test_a_BUSY_gpu_is_not_offered():
    """The gate is capacity ONE. Routing a summarise at the GPU mid-loop does
    not run it fast, it queues it - and the census waits behind its own
    condense call."""
    nodes = {"gpu-250": _n(True, 1), "cpu-246": _n(False, 0)}
    assert RP.free_gpu(nodes) == ""


def test_a_cpu_node_is_never_offered_as_a_gpu():
    assert RP.free_gpu({"cpu-246": _n(False, 0)}) == ""


def test_an_offline_gpu_is_not_offered():
    assert RP.free_gpu({"gpu-250": _n(True, 0, status="offline")}) == ""


def test_an_unreadable_load_is_not_treated_as_idle():
    """Guessing wrong here queues a background job in front of a loop."""
    assert RP.free_gpu({"gpu-250": {"has_gpu": True, "in_use": "?"}}) == ""


def test_no_nodes_at_all():
    assert RP.free_gpu({}) == ""
    assert RP.free_gpu(None) == ""


# ── rule reading ────────────────────────────────────────────────────────────
def test_the_flags_are_read_off_the_rule():
    assert RP.wants_free_gpu({"prefer_gpu_if_free": True}) is True
    assert RP.wants_free_gpu({"prefer_gpu_if_free": False}) is False
    assert RP.wants_free_gpu({}) is False
    assert RP.wants_free_gpu(None) is False
    assert RP.preferred_of({"prefer": "cpu-246"}) == "cpu-246"
    assert RP.preferred_of({}) == ""
    assert RP.preferred_of(None) == ""


# ── the case that started it ────────────────────────────────────────────────
def test_summarize_and_embedding_end_up_on_different_nodes_when_both_idle():
    """The intended split: each node favours a class, neither is excluded."""
    nodes = ["cpu-246", "cpu-247"]
    pick = lambda prefer: min(nodes, key=lambda i: _score(i, 0, prefer))
    assert pick("cpu-246") == "cpu-246"      # embedding
    assert pick("cpu-247") == "cpu-247"      # naming / summarize / dream_director


def test_a_saturated_preferred_node_hands_work_to_the_other_one():
    """The behaviour cpu-247 needed and did not have."""
    load = {"cpu-246": 0, "cpu-247": 2}
    chosen = min(load, key=lambda i: _score(i, load[i], "cpu-247"))
    assert chosen == "cpu-246"
