"""What a chat turn needs fetched before the model answers (vera/agents/turn_needs_core.py) - the rules stage.

Cases follow the baseline query set (shared-planning context-and-ui tools/queries.json): fresh-facts questions want
the web; questions about Vera's own work and references to the past want memory, not the web.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import turn_needs_core as tn  # noqa: E402


@pytest.mark.parametrize("q", [
    "what is the bitcoin price today", "latest news about nvidia earnings", "who won the match last night",
    "current weather in london", "what happened in the markets this week", "newest ollama release notes",
    "eth gas fees right now", "today's top headlines", "is there an outage at github today",
    "what do we know about the latest nvidia earnings", "check the bitcoin price again",
])
def test_fresh_facts_want_the_web(q):
    n = tn.needs(q)
    assert n["web"]["on"], (q, n)
    assert n["web"]["confidence"] >= 0.7 and n["web"]["query"]


@pytest.mark.parametrize("q", [
    "search the web for kokoro tts phoneme limits", "look up the qwen3.5 context window", "google gliner models",
])
def test_an_explicit_ask_always_wants_the_web(q):
    n = tn.needs(q)
    assert n["web"]["on"] and n["web"]["confidence"] >= 0.9 and n["web"]["reason"] == "asked to search"
    assert "search" not in n["web"]["query"].lower() or "search" in q.split("for")[-1]


@pytest.mark.parametrize("q", [
    "docker daemon outage during the backup window", "difference between RRF and linear score fusion",
    "how is the GPU gate configured for the census", "why did the agentic loop stall on the writer step",
    "the canvas items keep scrolling out of view", "what were the results of the latest census run",
    "what is a JEPA model", "write a haiku about autumn", "explain how HNSW vector indexes work",
])
def test_vera_work_and_general_knowledge_do_not_go_to_the_web(q):
    assert not tn.needs(q)["web"]["on"], q


@pytest.mark.parametrize("q", [
    "what did we decide about the canvas header last time", "remember the dashboard layout we discussed",
    "you said something about the dream director yesterday", "as before, summarise the estate review",
    "the research report on proxmox backups",
    "what were the results of the latest census run",
])
def test_references_to_the_past_want_a_deep_memory_search(q):
    assert tn.needs(q)["memory_deep"]["on"], q


def test_a_url_is_fetched_not_searched():
    assert not tn.needs("summarise https://example.com/post please")["web"]["on"]


def test_a_year_past_the_models_knowledge_wants_the_web():
    assert tn.needs("what changed in python 2026", knowledge_year=2024)["web"]["on"]
    assert not tn.needs("what changed in python 2023", knowledge_year=2024)["web"]["on"]


def test_the_query_drops_the_asking():
    assert tn.search_query("can you search the web for gliner zero-shot labels please") == "gliner zero-shot labels"


def test_empty_text_needs_nothing():
    n = tn.needs("")
    assert not n["web"]["on"] and not n["memory_deep"]["on"]


# ── the deep memory query is the topic, not the question (prod 2026-10-04: the whole question took 10.3 s on the
#    text tier and found nothing inside the 1.5 s budget; its topic took 0.2 s and found five) ──────────────────────
@pytest.mark.parametrize("q, topic", [
    ("What did we decide last time about the memory half-life?", "memory half-life"),
    ("remember the plan for the census calendar?", "census calendar"),
    ("you said something earlier about GLiNER labels", "something GLiNER labels"),
    ("what was the decision on the vector store", "vector store"),
])
def test_the_memory_query_is_the_topic(q, topic):
    n = tn.needs(q)
    assert n["memory_deep"]["on"], (q, n)
    assert n["memory_deep"]["query"] == topic


def test_a_memory_query_with_nothing_left_falls_back_to_the_words():
    assert tn.memory_query("what did we say last time?") == "what did we say last time?"
    assert tn.memory_query("") == ""