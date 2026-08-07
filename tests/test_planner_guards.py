"""Planner GUARD regression tests — lock the behaviour behind the two live
planner-drift incidents (see documentation/postmortems/2026-08-06-agentic-loop-
planner-drift.md). Pure/deterministic: they import vera.dag.planner_core, which
dag_workshop_capabilities.py also imports, so they guard the code that runs.
"""

import os

from vera.dag import planner_core as P


# ── plan_words: stemming + stopword removal ─────────────────────────────────

def test_plan_words_stems_and_strips_stopwords():
    w = P.plan_words("Create a detailed report on Pokemon sprites")
    assert "create" not in w and "detailed" not in w and "report" not in w  # stopwords
    assert "pokemon" in w
    assert "sprite" in w          # 'sprites' -> 'sprite' (plural stem)
    assert P.plan_words("") == set()


# ── filter_skills_for_goal: the crypto-hijack guard (incident #1) ───────────

_SKILLS = [
    {"id": "ecf9c33c", "name": "Maxhodl",
     "description": "knowledgeable about cryptocurrency, Bitcoin, DeFi, airdrops"},
    {"id": "f8b63623", "name": "A Pokemon Guide", "description": "gen 1 pokemon expert"},
    {"id": "sys-exec-fileio", "description": "how to create/read/edit files"},
    {"id": "fmt-report", "description": "output format profile"},
    {"id": "abc123", "name": "charting", "description": "draw charts",
     "applies_to_caps": ["markets.infographic.save"]},
]


def test_unrelated_crypto_skill_dropped_for_non_crypto_goal():
    # NOTE: the filter is purely LEXICAL (shared content word). "pokemon" in the
    # goal is what keeps the pokemon skill — a bare "pokedex" goal would NOT,
    # since 'pokedex' and 'pokemon' don't share a stem. That lexical strictness
    # (it can drop a genuinely-relevant skill that uses different words) is a
    # known follow-up; this test locks the CURRENT behaviour.
    kept = {s["id"] for s in P.filter_skills_for_goal(
        _SKILLS, "create a gen1 pokemon pokedex in html")}
    assert "ecf9c33c" not in kept          # crypto skill HIJACKER — must be dropped
    assert "f8b63623" in kept              # pokemon skill — shares 'pokemon', kept
    assert "sys-exec-fileio" in kept and "fmt-report" in kept  # structural — always kept


def test_crypto_skill_kept_for_a_crypto_goal():
    kept = {s["id"] for s in P.filter_skills_for_goal(_SKILLS, "research cryptocurrency and DeFi trends")}
    assert "ecf9c33c" in kept              # now genuinely relevant → kept


def test_skill_kept_when_it_teaches_a_catalog_cap():
    kept = {s["id"] for s in P.filter_skills_for_goal(
        _SKILLS, "make a birthday card", catalog={"markets.infographic.save"})}
    assert "abc123" in kept                # teaches a cap in the catalog → kept
    assert "ecf9c33c" not in kept          # still irrelevant, no catalog overlap


# ── plan_drifted: the drift detector (incident #2) ──────────────────────────

def test_drift_detects_wholesale_topic_swap():
    goal = "get the latest developments in AI and ML and create a report"
    crypto = [{"title": "research crypto fundamentals"},
              {"title": "analyze trading strategies"},
              {"title": "cover DeFi and airdrops"}]
    assert P.plan_drifted(goal, crypto) is True


def test_drift_does_not_fire_on_an_on_topic_plan():
    goal = "get the latest developments in AI and ML and create a report"
    good = [{"title": "search web for latest AI and ML developments"},
            {"title": "summarize the research findings"}]
    assert P.plan_drifted(goal, good) is False


def test_drift_never_judges_a_too_short_goal():
    # A goal with <2 content words has nothing to judge against → never drift.
    assert P.plan_drifted("get ml", [{"title": "research crypto"}]) is False
    assert P.plan_drifted("x", [{"title": "anything"}]) is False


# ── planner_sampling: the determinism fix (incident #2) ─────────────────────

def test_planner_sampling_is_nondeterministic_with_fresh_seed():
    a = P.planner_sampling()
    b = P.planner_sampling()
    assert a["temperature"] == P.PLANNER_TEMP > 0    # real temperature, not greedy 0
    assert a["top_p"] == 0.95
    assert "seed" in a and "seed" in b
    assert a["seed"] != b["seed"]                    # FRESH per call → retries differ


def test_planner_sampling_respects_explicit_base_temperature():
    opts = P.planner_sampling({"temperature": 0.1})
    assert opts["temperature"] == 0.1                # caller's explicit temp wins
    assert "seed" in opts                            # but still gets a fresh seed


def test_planner_sampling_disabled_returns_base_unchanged():
    # With non-determinism off, the base is returned untouched (deterministic).
    saved = P.PLANNER_NONDET
    try:
        P.PLANNER_NONDET = False
        base = {"temperature": 0}
        assert P.planner_sampling(base) is base
    finally:
        P.PLANNER_NONDET = saved
