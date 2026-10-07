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


# ── rule reading ────────────────────────────────────────────────────────────
def test_the_preference_is_read_off_the_rule():
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


# â”€â”€ the fields must SURVIVE the config round-trip â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Nearly shipped broken: ollama.routing.save normalises every rule through
# _rule(), which takes a fixed keyword list. A rule carrying `prefer` or
# `prefer` would have been accepted, silently stripped, and the routing would
# have looked configured while behaving exactly as before.

@pytest.fixture(scope="module")
def orch():
    return pytest.importorskip(
        "vera.capability_orchestration",
        reason="orchestrator not importable here (needs the app env)")


def test_rule_carries_the_prefer_field(orch):
    r = orch._rule("naming", deny_gpu=True, prefer="cpu-247")
    assert r["prefer"] == "cpu-247"


def test_rule_defaults_prefer_off(orch):
    r = orch._rule("chat", prefer_gpu=True)
    assert r["prefer"] == ""


def test_the_defaults_split_the_cpu_work(orch):
    """User 2026-09-28: the GPU node's CPU sibling is the primary embedder (and
    takes the small naming model), so the two CPU nodes are free for the large
    models - cpu-247 keeps the long-horizon work."""
    d = orch.DEFAULT_ROUTING_RULES
    assert d["embedding"]["prefer"] == "gpu-250-cpu"
    assert d["naming"]["prefer"] == "gpu-250-cpu"
    # summarize is NOT here: it left the CPU entirely (GPU-only, see below).
    for jt in ("dream_director", "research_reader"):
        assert d[jt]["prefer"] == "cpu-247", jt


def test_the_hard_exclusion_is_gone_from_the_rebalanced_rules(orch):
    """avoid_embed collapsed the candidate list to one node before load was
    considered. A soft preference replaces it - if this comes back, so does the
    saturation."""
    d = orch.DEFAULT_ROUTING_RULES
    for jt in ("naming", "dream_director"):
        assert not d[jt]["avoid_embed"], jt


def test_summarize_is_GPU_ONLY_and_off_the_cpu_nodes(orch):
    """It runs INLINE - the caller is blocked awaiting it - so a CPU summarise
    overlaps nothing: the GPU idles while the slower box works and the caller
    just waits longer. Safe because the gate lease is per-GENERATION, so an
    executor's lease is released before its condense begins."""
    r = orch.DEFAULT_ROUTING_RULES["summarize"]
    assert r["prefer_gpu"] is True
    assert r["allow"] == ["gpu-*"]
    assert r["deny_gpu"] is False


def test_the_loop_condense_actually_carries_the_job_type(orch):
    """Without job_type the condense inherited the EXECUTOR's routing and the
    summarize rule never applied to it - the one place it matters most."""
    import pathlib
    src = pathlib.Path(orch.__file__).parent / "dag" / "dag_workshop_capabilities.py"
    body = src.read_text(encoding="utf-8")
    i = body.index("async def _v5_condense_output")
    assert 'job_type="summarize"' in body[i:i + 4000]
