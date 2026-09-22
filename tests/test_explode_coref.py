# -*- coding: utf-8 -*-
"""coref (EXPLODE.md §4, S7): one entity, however it is written.

The extractor already gives one card per NORMALISED name, so "Northwind" twice is one card. What it cannot see is
that "Alice Carter", "Carter" and "she" are the same person, or that "CMA" is the Competition and Markets
Authority -- so a passage draws six cards where there are three entities, and the relations hang off whichever
spelling happened to be in the sentence. This is a heuristic and says so on every merge; ambiguity is a reason to
do nothing.
"""
import asyncio

import pytest

from vera.research import explode_capabilities as X

PASSAGE = (
    "Alice Carter, chief executive of Northwind Ltd, said on Tuesday that the company will acquire Contoso. "
    "Carter said she expects the deal to close in June.\n\n"
    "Contoso was founded by Bob Ng in 2015. Ng will join the board after the acquisition closes.\n\n"
    "The deal is pending approval by the Competition and Markets Authority. The CMA has not commented."
)


def at(name, start=None):
    """A card the way the entity layer makes one, standing at the name's first mention."""
    s = PASSAGE.index(name) if start is None else start
    return {"start": s, "end": s + len(name)}


def card(cid, title, kind, start=None):
    return {"id": cid, "group": "p1", "layer": "ner", "kind": kind, "title": title,
            "subtitle": kind.upper() + " · 1 mention", "span": dict(at(title, start), path=""),
            "fields": [], "badges": [], "by": "test"}


def ctx_of(cards, edges=None):
    return {"text": PASSAGE, "base": 0, "path": "", "content_type": "text",
            "paragraphs": X.paragraphs_of(PASSAGE, 0), "sentences": X.sentences_of(PASSAGE, 0),
            "cards": list(cards), "edges": list(edges or []), "assessments": [], "ent_list": []}


def run(ctx):
    return asyncio.get_event_loop().run_until_complete(X._layer_coref(ctx))


def test_a_surname_is_the_person_and_an_acronym_is_the_body():
    cards = [card("e1", "Alice Carter", "person"), card("e2", "Carter", "person"),
             card("e3", "Northwind Ltd", "org"), card("e4", "Northwind", "org", PASSAGE.index("Northwind Ltd")),
             card("e5", "Competition and Markets Authority", "org"), card("e6", "CMA", "org")]
    ctx = ctx_of(cards)
    out = run(ctx)
    left = {c["title"] for c in ctx["cards"]}
    assert "Alice Carter" in left and "Carter" not in left
    assert "Competition and Markets Authority" in left and "CMA" not in left
    head = next(c for c in ctx["cards"] if c["title"] == "Alice Carter")
    assert [m["by"] for m in head["mentions"] if m["by"] == "shortened"]
    assert "Carter" in [f["v"] for f in head["fields"] if f["k"] == "also"][0]
    cma = next(c for c in ctx["cards"] if c["title"].startswith("Competition"))
    assert any(m["by"] == "acronym" and m["text"] == "CMA" for m in cma["mentions"])
    assert out["count"] >= 3 and "folded in" in out["note"]


def test_two_people_who_share_a_surname_leave_it_alone():
    """The whole risk of this pass: a bare surname with two candidates is not evidence, it is ambiguity."""
    cards = [card("e1", "Alice Carter", "person"), card("e2", "Carter", "person"),
             {"id": "e3", "group": "p1", "layer": "ner", "kind": "person", "title": "Bob Carter",
              "subtitle": "", "span": {"path": "", "start": 5, "end": 15}, "fields": [], "badges": [], "by": "t"}]
    ctx = ctx_of(cards)
    run(ctx)
    assert {c["title"] for c in ctx["cards"]} == {"Alice Carter", "Carter", "Bob Carter"}


def test_a_name_of_another_type_is_not_the_same_thing():
    cards = [card("e1", "Bristol Airport", "place", 0), card("e2", "Bristol", "person", 40)]
    ctx = ctx_of(cards)
    run(ctx)
    assert len(ctx["cards"]) == 2


def test_a_relation_follows_its_entity_rather_than_dangling():
    cards = [card("e1", "Alice Carter", "person"), card("e2", "Carter", "person"), card("e3", "Contoso", "org")]
    edges = [{"from": "e2", "to": "e3", "kind": "RELATES", "layer": "rel.typed", "resolution": "exact"},
             {"from": "e1", "to": "e3", "kind": "RELATES", "layer": "rel.typed", "resolution": "exact"}]
    ctx = ctx_of(cards, edges)
    run(ctx)
    ids = {c["id"] for c in ctx["cards"]}
    assert all(e["from"] in ids and e["to"] in ids for e in ctx["edges"]), ctx["edges"]
    assert len(ctx["edges"]) == 1                      # the two became the same relation, kept once
    assert ctx["edges"][0]["from"] == "e1"


def test_the_pronouns_that_stand_for_an_entity_are_its_mentions_not_cards_of_their_own():
    cards = [card("e1", "Alice Carter", "person"), card("e2", "Carter", "person")]
    ctx = ctx_of(cards)
    out = run(ctx)
    head = next(c for c in ctx["cards"] if c["title"] == "Alice Carter")
    pron = [m for m in head["mentions"] if m["by"] == "pronoun"]
    assert pron and all(m["text"].lower() in ("she", "her", "hers") for m in pron), head["mentions"]
    assert all(PASSAGE[m["start"]:m["end"]].lower() == m["text"].lower() for m in pron)   # the span is the word
    assert len(ctx["cards"]) == 1 and "by pronoun" in head["subtitle"]
    assert out["count"] >= 2


def test_a_pronoun_after_someone_else_is_not_taken():
    """'he' following Bob Ng is Ng's, not Carter's -- an entity of the same type in between stops the reach."""
    text = "Alice Carter spoke. Bob Ng replied, and he left."
    cards = [{"id": "a", "group": None, "layer": "ner", "kind": "person", "title": "Alice Carter",
              "subtitle": "", "span": {"path": "", "start": 0, "end": 12}, "fields": [], "badges": [], "by": "t"},
             {"id": "b", "group": None, "layer": "ner", "kind": "person", "title": "Bob Ng",
              "subtitle": "", "span": {"path": "", "start": 20, "end": 26}, "fields": [], "badges": [], "by": "t"}]
    ctx = {"text": text, "base": 0, "path": "", "content_type": "text", "paragraphs": X.paragraphs_of(text, 0),
           "sentences": X.sentences_of(text, 0), "cards": cards, "edges": [], "assessments": [], "ent_list": []}
    run(ctx)
    alice = next(c for c in ctx["cards"] if c["title"] == "Alice Carter")
    bob = next(c for c in ctx["cards"] if c["title"] == "Bob Ng")
    assert not [m for m in (alice.get("mentions") or []) if m["by"] == "pronoun"]
    assert [m["text"] for m in (bob.get("mentions") or []) if m["by"] == "pronoun"] == ["he"]


def test_the_layer_is_registered_after_the_relations_and_says_what_it_is():
    ids = [l["id"] for l in X.layer_list()]
    assert "coref" in ids and ids.index("coref") > ids.index("rel.typed")
    row = next(l for l in X.layer_list() if l["id"] == "coref")
    assert row["default_on"] is True and row["needs"] == ["ner"] and "heuristic" in row["by"]
    assert "ambiguity" in row["note"]


def test_the_clusters_are_a_pure_function_anyone_can_check():
    cards = [card("e1", "Alice Carter", "person"), card("e2", "Carter", "person")]
    cl = X.coref_clusters(cards)
    assert cl == {"e2": {"head": "e1", "why": "shortened"}}
    assert X.coref_clusters([card("e1", "Alice Carter", "person")]) == {}
